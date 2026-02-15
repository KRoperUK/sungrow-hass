import json
import os
import sys

def sort_manifest(manifest_path):
    with open(manifest_path, 'r') as f:
        data = json.load(f)

    # Define strict order for specific keys
    ordered_keys = ["domain", "name"]
    
    # Create new dict with ordered keys first, then sorted remainder
    new_data = {}
    for key in ordered_keys:
        if key in data:
            new_data[key] = data.pop(key)
    
    # Sort remaining keys alphabetically
    for key in sorted(data.keys()):
        new_data[key] = data[key]

    # Write back
    with open(manifest_path, 'w') as f:
        json.dump(new_data, f, indent=2)
        f.write('\n') # Add trailing newline

def main():
    manifest_path = "custom_components/sungrow/manifest.json"
    if not os.path.exists(manifest_path):
        print(f"Manifest not found at {manifest_path}")
        sys.exit(1)
        
    sort_manifest(manifest_path)
    print("Manifest sorted successfully.")

if __name__ == "__main__":
    main()
