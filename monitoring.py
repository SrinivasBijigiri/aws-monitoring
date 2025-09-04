import boto3
import datetime
import time
import pytz

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
    """Get the true MAX CPU utilization (%) in the last 12 hours from CloudWatch"""
    try:
        utc_now = datetime.datetime.now(pytz.UTC)
        start = utc_now - datetime.timedelta(hours=12)

        response = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "cpu_max",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/EC2",
                            "MetricName": "CPUUtilization",
                            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                        },
                        "Period": 60,  # 1-minute granularity
                        "Stat": "Maximum",
                        "Unit": "Percent",
                    },
                    "ReturnData": True,
                }
            ],

            StartTime=start,
            EndTime=utc_now,
            ScanBy="TimestampDescending",
        )

        results = response.get("MetricDataResults", [])
        if not results or not results[0]["Values"]:
            return None

        # Return the highest CPU value in last 12 hours
        return max(results[0]["Values"])

    except Exception as e:
        print(f"⚠ Error fetching CPU for {instance_id}: {e}")
        return None


def check_storage(instance_id):
    """Check /data storage usage (%) via SSM with retry"""
    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "df -h /data | awk 'NR==2 {print $5, $3, $2}'"
                ]
            },
        )
        command_id = response["Command"]["CommandId"]

        time.sleep(2)

        # retry up to 10 times with 3s delay
        for _ in range(10):
            output = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
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
        print(f"  ⚠ Error checking storage on {instance_id}: {e}")
        return None


def monitor_beanstalk():
    print("\n--- Elastic Beanstalk Monitoring ---\n")
    envs = eb.describe_environments()["Environments"]

    ok_count = 0
    issue_count = 0

    for env in envs:
        name = env["EnvironmentName"]
    # Skip suspended env
        if name == "kazam-app-backend-env":
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
            issues.append({
                "Type": "Elastic Beanstalk",
                "Name": name,
                "Metric": "Health",
                "Status": health,
            })

    print(f"Summary: {ok_count} environments OK, {issue_count} with issues\n")


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
        return instance_id  # fallback to instance ID if no Name tag
    except Exception as e:
        print(f"⚠ Could not fetch name for {instance_id}: {e}")
        return instance_id



def monitor_ec2():
    print("\n--- MongoDB EC2 Monitoring ---\n")

    # Logger Mongo: CPU only
    print("### Logger Mongo Instances (CPU only) ###")
    for inst in logger_mongo_instances:
        name = get_instance_name(inst)  # fetch instance name
        cpu = check_ec2_cpu(inst)
        if cpu is None:
            print(f"Instance: {name} - ⚠ No CPU data")
            issues.append({
                "Type": "EC2 Logger",
                "Name": name,
                "Metric": "CPU",
                "Status": "No data",
            })
        elif cpu > 65:
            print(f"Instance: {name}\n  ❌ CPU High: {cpu:.2f}% (max in last 12hrs)")
            issues.append({
                "Type": "EC2 Logger",
                "Name": name,
                "Metric": "CPU",
                "Status": f"High ({cpu:.2f}%)",
            })
        else:
            print(f"Instance: {name}\n  ✅ CPU OK: {cpu:.2f}% (max in last 12hrs)")
    print("")

    # Main Mongo: CPU + Storage
    print("### Main Mongo Instances (CPU + Storage) ###")
    for inst in main_mongo_instances:
        name = get_instance_name(inst)  # fetch instance name
        cpu = check_ec2_cpu(inst)
        if cpu is None:
            print(f"Instance: {name} - ⚠ No CPU data")
            issues.append({
                "Type": "EC2 Main Mongo",
                "Name": name,
                "Metric": "CPU",
                "Status": "No data",
            })
        elif cpu > 65:
            print(f"Instance: {name}\n  ❌ CPU High: {cpu:.2f}% (max in last 12hrs)")
            issues.append({
                "Type": "EC2 Main Mongo",
                "Name": name,
                "Metric": "CPU",
                "Status": f"High ({cpu:.2f}%)",
            })
        else:
            print(f"Instance: {name}\n  ✅ CPU OK: {cpu:.2f}% (max in last 12hrs)")

        storage = check_storage(inst)
        if storage:
            percent, used, total = storage
            if percent > 85:
                print(f"  ❌ Storage High: {percent:.2f}% used ({used}/{total})")
                issues.append({
                    "Type": "EC2 Main Mongo",
                    "Name": name,
                    "Metric": "Storage",
                    "Status": f"High ({percent:.2f}% used {used}/{total})",
                })
            else:
                print(f"  ✅ Storage OK: {percent:.2f}% used ({used}/{total})")
        else:
            print(f"  ⚠ Storage check failed")
            issues.append({
                "Type": "EC2 Main Mongo",
                "Name": name,
                "Metric": "Storage",
                "Status": "Check failed",
            })
    print("")


def print_issue_summary():
    print("\n--- ⚠ Issues Summary ---\n")
    if not issues:
        print("✅ No issues detected. All systems healthy.\n")
        return

    col_widths = {
        "Type": 20,
        "Name": 22,
        "Metric": 18,
        "Status": 28,
    }

    header = f"| {'Type':<{col_widths['Type']}} | {'Name/Instance ID':<{col_widths['Name']}} | {'Metric':<{col_widths['Metric']}} | {'Status/Value':<{col_widths['Status']}} |"
    line = "+" + "+".join(["-" * (w + 2) for w in col_widths.values()]) + "+"
    print(line)
    print(header)
    print(line)

    for issue in issues:
        row = f"| {issue['Type']:<{col_widths['Type']}} | {issue['Name']:<{col_widths['Name']}} | {issue['Metric']:<{col_widths['Metric']}} | {issue['Status']:<{col_widths['Status']}} |"
        print(row)

    print(line + "\n")


import smtplib
from email.mime.text import MIMEText

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


# --- New FOTA API Check ---
import requests

def check_fota_time_api():
    """Check if FOTA time API returns a valid epoch timestamp"""
    print("\n--- FOTA Time API Check ---\n")
    try:
        response = requests.get("https://fota.kazam.in/time", timeout=10)
        if response.status_code == 200:
            data = response.text.strip()
            if data.isdigit():
                print(f"✅ API is working. Response: {data}")
            else:
                print(f"❌ Invalid response: {data}")
                issues.append({
                    "Type": "FOTA API",
                    "Name": "fota.kazam.in/time",
                    "Metric": "Response",
                    "Status": "Invalid data"
                })
        else:
            print(f"❌ API returned status {response.status_code}")
            issues.append({
                "Type": "FOTA API",
                "Name": "fota.kazam.in/time",
                "Metric": "HTTP",
                "Status": f"Error {response.status_code}"
            })
    except Exception as e:
        print(f"❌ Error calling API: {e}")
        issues.append({
            "Type": "FOTA API",
            "Name": "fota.kazam.in/time",
            "Metric": "Exception",
            "Status": str(e)
        })


# --- Main ---
if __name__ == "__main__":
    # Capture *all* printed output into a string
    from io import StringIO
    import sys

    buffer = StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buffer

    # Run monitoring
    monitor_beanstalk()
    monitor_ec2()
    check_fota_time_api()   # ✅ Added here
    print_issue_summary()

    # Restore stdout
    sys.stdout = sys_stdout
    full_report = buffer.getvalue()

    # Send email with full details (CPU, Storage, Summary)
    send_email(
        subject="⚠ AWS Monitoring Report",
        body=full_report,
        to_email="bijigiri@kazam.in"
    )

    # Also print locally (optional, for logs)
    print(full_report)
