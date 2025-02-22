import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
# File paths
file_paths = {
    "Th = 1.09V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.09V.csv",
    "Th = 1.08V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.08V.csv",
    "Th = 1.07V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.07V.csv",
    "Th = 1.06V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.06V.csv",
    "Th = 1.05V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.05V.csv",
    "Th = 1.04V": r"C:\Users\alex\Documents\IKP\HYDRA\FiberDetector\KiCAD\Test_PCB_MPPC-array_FARICH\Spice\python\Data\Diffrent_Th_Values\ToT_560nH_200uC-2mC_Th_1.04V.csv",
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
plt.xlim(1.8, 2.6)
plt.xlabel("ToT (ns)")
#plt.ylabel("Frequency")
plt.title("ToT for different Th. values")
plt.legend()
plt.grid(linestyle='--', alpha=0.7)

# Show plot
plt.show()
