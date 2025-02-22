import pandas as pd
import re
import tkinter as tk
from tkinter import filedialog

def extract_tot_from_log(log_file_path, save_csv_path):
    """Extracts only ToT measurements from an LTspice log file and saves to CSV."""
    tot_data = []
    parsing_tot = False

    with open(log_file_path, "r") as file:
        for line in file:
            # Start capturing when "Measurement: tot" is found
            if "Measurement: tot" in line:
                parsing_tot = True
                continue  # Skip to next line

            # Stop capturing when another measurement starts
            if parsing_tot and ("Measurement:" in line and "tot" not in line):
                break  # Stop capturing when another measurement appears

            # Extract numeric values from ToT measurements
            if parsing_tot:
                match = re.match(r"\s*(\d+)\s+([\d.eE+-]+)", line)
                if match:
                    step = int(match.group(1))
                    tot_value = float(match.group(2))
                    tot_data.append((step, tot_value))

    # Convert to DataFrame
    tot_df = pd.DataFrame(tot_data, columns=["Step", "ToT (s)"])
    
    # Save as CSV
    tot_df.to_csv(save_csv_path, index=False)
    print(f"ToT data successfully saved to: {save_csv_path}")

# Create a file dialog for selecting the log file
root = tk.Tk()
root.withdraw()  # Hide the root window

log_file_path = filedialog.askopenfilename(title="Select LTspice Log File", filetypes=[("Log Files", "*.log"), ("All Files", "*.*")])
if not log_file_path:
    print("No file selected. Exiting...")
    exit()

# Create a file dialog for selecting the save location
save_csv_path = filedialog.asksaveasfilename(title="Save ToT Data As", defaultextension=".csv", filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
if not save_csv_path:
    print("No save location selected. Exiting...")
    exit()

# Run the extraction function
extract_tot_from_log(log_file_path, save_csv_path)
