from datetime import datetime

# Create filename with timestamp
timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
filename = f"generated_{timestamp}.txt"

content = f"""
File generated automatically by GitHub Actions
UTC Time: {datetime.utcnow()}
"""

with open(filename, "w") as f:
    f.write(content.strip())

print(f"File created: {filename}")
