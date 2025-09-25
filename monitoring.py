import boto3
import datetime
import time
import pytz
import requests
from io import StringIO
import sys
import smtplib
from email.mime.text import MIMEText
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tabulate import tabulate

# --- AWS Clients ---
eb = boto3.client("elasticbeanstalk", region_name="ap-south-1")
cw = boto3.client("cloudwatch", region_name="ap-south-1")
ssm = boto3.client("ssm", region_name="ap-south-1")

# --- Instances ---
logger_mongo_instances = [
    "i-00e0f35f25480f647",
    "i-0c88e356ad88357b0",
    "i-070ed38555e983a39",
]

main_mongo_instances = [
    "i-0fd0bddfa1f458b4b",
    "i-0b3819ce528f9cd9f",
    "i-051daf3ab8bc94e62",
]

# --- Issues collector ---
issues = []

# --- Functions ---

def check_ec2_cpu(instance_id):
    """Get the AVG CPU utilization (%) in the last 6 hours from CloudWatch"""
    try:
        utc_now = datetime.datetime.now(pytz.UTC)
        start = utc_now - datetime.timedelta(hours=6)

        response = cw.get_metric_data(
            MetricDataQueries=[{
                "Id": "cpu_avg",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                    },
                    "Period": 300,        # 5-min granularity (same as console default)
                    "Stat": "Average",    # Average instead of Maximum
                    "Unit": "Percent",
                },
                "ReturnData": True,
            }],
            StartTime=start,
            EndTime=utc_now,
            ScanBy="TimestampDescending",
        )

        results = response.get("MetricDataResults", [])
        if not results or not results[0]["Values"]:
            return None
        return max(results[0]["Values"])   # or sum(...) / len(...) if you want real avg
    except Exception as e:
        print(f"⚠ Error fetching CPU for {instance_id}: {e}")
        return None


def check_storage(instance_id, path="/data"):
    """Check storage usage (%) via SSM for a given path"""
    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [f"df -h {path} | awk 'NR==2 {{print $5, $3, $2}}'"]},
        )
        command_id = response["Command"]["CommandId"]
        time.sleep(2)
        for _ in range(10):
            output = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            if output["Status"] in ["InProgress", "Pending"]:
                time.sleep(3)
                continue
            if output["Status"] == "Success":
                result = output["StandardOutputContent"].strip()
                if result:
                    parts = result.split()
                    if len(parts) == 3:
                        percent = parts[0].replace("%", "")
                        used = parts[1]
                        total = parts[2]
                        return float(percent), used, total
                break
        return None
    except Exception as e:
        print(f"⚠ Error checking storage on {instance_id} ({path}): {e}")
        return None

def get_instance_name(instance_id):
    """Fetch the Name tag of an EC2 instance"""
    try:
        ec2 = boto3.client("ec2", region_name="ap-south-1")
        response = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])
        if reservations:
            instance = reservations[0]["Instances"][0]
            tags = instance.get("Tags", [])
            for tag in tags:
                if tag["Key"] == "Name":
                    return tag["Value"]
        return instance_id
    except Exception as e:
        print(f"⚠ Could not fetch name for {instance_id}: {e}")
        return instance_id

# --- Elastic Beanstalk Monitoring ---
def monitor_beanstalk():
    print("\n--- Elastic Beanstalk Monitoring ---\n")
    envs = eb.describe_environments()["Environments"]
    ok_count = 0
    issue_count = 0
    for env in envs:
        name = env["EnvironmentName"]
        if name in ("kazam-app-backend-env", "Kazam-platform-replica-env"):  # skip suspended envs
            continue
        status = env["Status"]
        health = env["Health"]
        print(f"Environment: {name}")
        print(f"  Status: {status}")
        print(f"  Health: {health}")
        if health == "Green":
            print("  ✅ All instances are healthy (OK)\n")
            ok_count += 1
        else:
            print("  ⚠ Some instances are not healthy!\n")
            issue_count += 1
            issues.append({"Type": "Elastic Beanstalk", "Name": name, "Metric": "Health", "Status": health})
    print(f"Summary: {ok_count} environments OK, {issue_count} with issues\n")

# --- EC2 Monitoring ---
def monitor_ec2():
    print("\n--- MongoDB EC2 Monitoring ---\n")

    def check_instances(instances, inst_type, storage_path="/data"):
        for inst in instances:
            name = get_instance_name(inst)
            cpu = check_ec2_cpu(inst)
            if cpu is None:
                print(f"Instance: {name} - ⚠ No CPU data")
                issues.append({"Type": inst_type, "Name": name, "Metric": "CPU", "Status": "No data"})
            elif cpu > 65:
                print(f"Instance: {name}\n  ❌ CPU High: {cpu:.2f}% (max in last 6hrs)")
                issues.append({"Type": inst_type, "Name": name, "Metric": "CPU", "Status": f"High ({cpu:.2f}%)"})
            else:
                print(f"Instance: {name}\n  ✅ CPU OK: {cpu:.2f}% (max in last 6hrs)")

            # Storage check
            storage = check_storage(inst, path=storage_path)
            if storage:
                percent, used, total = storage
                if percent > 84:
                    print(f"  ❌ Storage High: {percent:.2f}% used ({used}/{total})")
                    issues.append({"Type": inst_type, "Name": name, "Metric": "Storage", "Status": f"High ({percent:.2f}% used {used}/{total})"})
                else:
                    print(f"  ✅ Storage OK: {percent:.2f}% used ({used}/{total})")
            else:
                print(f"  ⚠ Storage check failed")
                issues.append({"Type": inst_type, "Name": name, "Metric": "Storage", "Status": "Check failed"})
            print("")

    print("### Logger Mongo Instances (CPU + Storage) ###")
    check_instances(logger_mongo_instances, "EC2 Logger", storage_path="/")  # root for logger

    print("### Main Mongo Instances (CPU + Storage) ###")
    check_instances(main_mongo_instances, "EC2 Main Mongo", storage_path="/data")  # /data for main
    print("")

# --- MQTT Nodes Monitoring via Selenium ---
MQTT_USERNAME = "devops"
MQTT_PASSWORD = "GauravDevops@#123"
MQTT_URL = "https://dashboard.mqtt.kazam.in/#/login"

def monitor_mqtt_nodes():
    print("\n--- MQTT Nodes Status ---\n")
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        driver = webdriver.Chrome(options=options)
        driver.get(MQTT_URL)

        wait = WebDriverWait(driver, 15)
        username_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[placeholder='Username']")))
        password_input = driver.find_element(By.CSS_SELECTOR, "input[placeholder='Password']")
        username_input.send_keys(MQTT_USERNAME)
        password_input.send_keys(MQTT_PASSWORD)

        login_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.el-button.el-button--primary")))
        login_button.click()

        nodes_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//li[normalize-space()='Nodes']")))
        nodes_tab.click()
        time.sleep(3)

        rows = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "div.el-table__body-wrapper table.el-table__body tr.el-table__row")
        ))

        table_data = []
        for row in rows:
            cols = row.find_elements(By.CSS_SELECTOR, ".cell")
            if len(cols) >= 7:
                node_name = cols[0].text
                memory = cols[5].text
                cpu_load = cols[6].text
                table_data.append([node_name, memory, cpu_load])

        driver.quit()

        if table_data:
            print(tabulate(table_data, headers=["Node Name", "Memory", "CPU Load"], tablefmt="grid"))
            for node_name, memory, cpu_load in table_data:
                issues.append({"Type": "MQTT Node", "Name": node_name, "Metric": "Memory/CPU", "Status": f"Memory: {memory}, CPU: {cpu_load}"})
        else:
            print("⚠ No MQTT nodes found.")
    except Exception as e:
        print(f"⚠ Error monitoring MQTT nodes: {e}")
        issues.append({"Type": "MQTT Node", "Name": "All", "Metric": "Connection", "Status": f"Failed: {e}"})

# --- FOTA API Check ---
def check_fota_time_api():
    print("\n--- FOTA Time API Check ---\n")
    try:
        response = requests.get("https://fota.kazam.in/time", timeout=10)
        if response.status_code == 200 and response.text.strip().isdigit():
            print(f"✅ API is working. Response: {response.text.strip()}")
        else:
            print(f"❌ Invalid response: {response.text.strip()}")
            issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Response", "Status": "Invalid data"})
    except Exception as e:
        print(f"❌ Error calling API: {e}")
        issues.append({"Type": "FOTA API", "Name": "fota.kazam.in/time", "Metric": "Exception", "Status": str(e)})

# --- Print Issues Summary ---
def print_issue_summary():
    filtered_issues = [i for i in issues if i['Type'] != "MQTT Node"]

    print("\n--- ⚠ Issues Summary ---\n")

    if not filtered_issues:
        print("✅ No issues detected. All systems healthy.\n")
        return

    col_widths = {"Type": 20, "Name": 22, "Metric": 18, "Status": 28}
    header = f"| {'Type':<{col_widths['Type']}} | {'Name/Instance ID':<{col_widths['Name']}} | {'Metric':<{col_widths['Metric']}} | {'Status/Value':<{col_widths['Status']}} |"
    line = "+" + "+".join(["-" * (w + 2) for w in col_widths.values()]) + "+"

    print(line)
    print(header)
    print(line)
    for issue in filtered_issues:
        row = f"| {issue['Type']:<{col_widths['Type']}} | {issue['Name']:<{col_widths['Name']}} | {issue['Metric']:<{col_widths['Metric']}} | {issue['Status']:<{col_widths['Status']}} |"
        print(row)
    print(line + "\n")

# --- Send Email ---
def send_email(subject, body, to_email):
    from_email = "bijigiri@kazam.in"
    password = "sauccvfzcztvenqp"  # your app-password
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(from_email, password)
        server.send_message(msg)

# --- Main ---
if __name__ == "__main__":
    buffer = StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buffer

    monitor_beanstalk()
    monitor_ec2()
    check_fota_time_api()
    monitor_mqtt_nodes()
    print_issue_summary()

    sys.stdout = sys_stdout
    full_report = buffer.getvalue()

    send_email(subject="⚠ AWS & MQTT Monitoring Report", body=full_report, to_email="bijigiri@kazam.in")
    print(full_report)
