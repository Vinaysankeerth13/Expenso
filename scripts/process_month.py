import json
import argparse
import sys
import os
sys.path.append(os.getcwd())
from app.transaction_event import extract_transaction

def process_file(input_path, output_path):
    """Reads raw email JSONs from input, extracts transaction events, and writes them to output."""
    with open(input_path, "r", encoding="utf-8") as f:
        emails = json.load(f)
    
    events = []
    print(f"Processing {len(emails)} emails from {input_path}...")
    
    for email in emails:
        event = extract_transaction(email)
        events.append(event)
        
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
        
    print(f"Saved {len(events)} events to {output_path}")
    
    # Print stats
    success = sum(1 for e in events if e["status"] == "success")
    failed = sum(1 for e in events if e["status"] == "failed")
    unknown = sum(1 for e in events if e["status"] == "unknown")
    print(f"Stats: Success={success}, Failed={failed}, Unknown={unknown}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    
    process_file(args.input, args.output)
