import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
# File paths
file_paths = {
    "1 pF": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_C7_Values\ToT_560nH_200uC-2mC_Th_1.04V_C7_1p.csv",
    "2.5 pF": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_C7_Values\ToT_560nH_200uC-2mC_Th_1.04V_C7_2.5p.csv",
    "5 pF": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_C7_Values\ToT_560nH_200uC-2mC_Th_1.04V_C7_5p.csv",
    "7.5 pF": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_C7_Values\ToT_560nH_200uC-2mC_Th_1.04V_C7_7.5p.csv",
    "10 pF": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_C7_Values\ToT_560nH_200uC-2mC_Th_1.04V_C7_10p.csv",
}

# Load data
datasets = {label: pd.read_csv(path).iloc[:, 1] * 1e9 for label, path in file_paths.items()}  # Convert ToT to ns

# Define common bin edges
bins = 256 # Number of bins
bin_edges = plt.hist(pd.concat(datasets.values()), bins=bins)[1]  # Get shared bin edges

# Create the histogram plot
plt.figure(figsize=(10, 6))

# Plot each dataset with the common bin edges
for label, tot_values in datasets.items():
    #plt.hist(tot_values, bins=bin_edges, alpha=0.3, label=label, histtype='stepfilled')
    sns.histplot(tot_values, bins=bin_edges,alpha=0.3, label=label,kde=False)

# Labels and title
plt.xlim(1.6, 3.6)
plt.xlabel("ToT (ns)")
#plt.ylabel("Frequency")
plt.title("ToT for Th=1.04 and different C7 values")
plt.legend()
plt.grid(linestyle='--', alpha=0.7)

# Show plot
plt.show()
