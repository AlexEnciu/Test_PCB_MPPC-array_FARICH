% ----- Define Folders -----
folder_noise = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\noise-0004';
folder_am241 = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\americiu_241';

files_noise = dir(fullfile(folder_noise, '*.mat'));
files_am241 = dir(fullfile(folder_am241, '*.mat'));

% ----- Allocate amplitude arrays -----
amplitudes_A_noise = zeros(length(files_noise), 1);
amplitudes_B_noise = zeros(length(files_noise), 1);
amplitudes_A_am241 = zeros(length(files_am241), 1);
amplitudes_B_am241 = zeros(length(files_am241), 1);

% ----- Load noise files -----
for i = 1:length(files_noise)
    data = load(fullfile(folder_noise, files_noise(i).name));
    amplitudes_A_noise(i) = max(data.A);
    amplitudes_B_noise(i) = max(data.B);
end

% ----- Load Am-241 files -----
for i = 1:length(files_am241)
    data = load(fullfile(folder_am241, files_am241(i).name));
    amplitudes_A_am241(i) = max(data.A);
    amplitudes_B_am241(i) = max(data.B);
end

% ===== Plot Channel A Histogram: Am-241 vs Noise =====
figure('Position', [100, 100, 1200, 600]);  % Left=100px, Bottom=100px, Width=1200px, Height=600px
hold on;
histogram(amplitudes_A_noise, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'k', ...
    'FaceAlpha', 0.4, ...
    'EdgeColor', 'none');
histogram(amplitudes_A_am241, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Noise - Channel A', 'U-238 - Channel A');
xlabel('Peak Amplitude [V]');
ylabel('Counts');
title('Channel A: Noise vs U-238');
grid on;
hold off;

% ===== Plot Channel B Histogram: Am-241 vs Noise =====
figure('Position', [100, 100, 1200, 600]);  % Left=100px, Bottom=100px, Width=1200px, Height=600px
hold on;
histogram(amplitudes_B_noise, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'k', ...
    'FaceAlpha', 0.4, ...
    'EdgeColor', 'none');
histogram(amplitudes_B_am241, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Noise - Channel B', 'U-238 - Channel B');
xlabel('Peak Amplitude [V]');
ylabel('Counts');
title('Channel B: Noise vs U-238');
grid on;
hold off;
