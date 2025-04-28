import os
import pandas as pd
import matplotlib.pyplot as plt
import re

# Step 1: Set the folder containing your CSV files
data_directory = "c:/Users/alex_/Documents/IKP/HYDRA/Test_PCB_MPPC-array_FARICH/SmallFiber/Demo_board_bga2803/osciloscope/Python/20250422-0006"

# Step 2: Get all CSV files in that folder
all_files = [os.path.join(data_directory, f) for f in os.listdir(data_directory) if f.endswith('.csv')]

# Step 3: Filter files that match your pattern: ..._001.csv to ..._999.csv
pattern = re.compile(r".*_\d{5}\.csv$")
filtered_csv_files = [f for f in all_files if pattern.match(os.path.basename(f))]

# Step 4: Read all files and extract peak amplitudes
amplitudes = []

for file in filtered_csv_files:
    try:
        df = pd.read_csv(
            file,
            skiprows=3,
            sep=';',
            names=['Time_us', 'Amplitude_mV'],
            decimal=','
        )
        peak = df['Amplitude_mV'].max()
        amplitudes.append(peak)
    except Exception as e:
        print(f"Error reading {file}: {e}")

# Step 5: Plot histogram
plt.figure(figsize=(10, 6))
plt.hist(amplitudes, bins=32, edgecolor='black')
plt.xlabel("Pulse Amplitude (mV)")
plt.ylabel("Counts")
plt.yscale('log')
plt.title("Amplitude Spectrum from MPPC + Scintillator + Am-241")
plt.grid(True)
plt.tight_layout()
plt.show()
