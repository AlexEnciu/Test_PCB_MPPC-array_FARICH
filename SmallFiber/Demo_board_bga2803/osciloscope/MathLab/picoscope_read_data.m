% Folder with .mat files
%folder = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\noise-0004';  % <<--- Set your path here
folder = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\Cosmics_1-0002';  % <<--- Set your path here
%folder = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\americiu_241';  % <<--- Set your path here

files = dir(fullfile(folder, '*.mat'));

% Store amplitudes
amplitudes_A = zeros(length(files), 1);
amplitudes_B = zeros(length(files), 1);

% Loop over each file
for i = 1:length(files)
    data = load(fullfile(folder, files(i).name));
    voltage_A = data.A;
    voltage_B = data.B;
    
    amplitudes_A(i) = max(voltage_A);
    amplitudes_B(i) = max(voltage_B);
end

% ---------- Histogram for Channel A ----------
figure('Position', [100, 100, 1200, 600]);  % Left=100px, Bottom=100px, Width=1200px, Height=600px
histogram(amplitudes_A, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'EdgeColor', 'none');
xlabel('Peak Amplitude [V]');
ylabel('Counts');
title('Histogram of Channel A');
grid on;

% ---------- Histogram for Channel B ----------
figure('Position', [100, 100, 1200, 600]);  % Left=100px, Bottom=100px, Width=1200px, Height=600px

histogram(amplitudes_B, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'EdgeColor', 'none');
xlabel('Peak Amplitude [V]');
ylabel('Counts');
title('Histogram of Channel B');
grid on;

% ---------- Overlapped Histogram ----------
figure('Position', [100, 100, 1200, 600]);  % Left=100px, Bottom=100px, Width=1200px, Height=600px

hold on;
histogram(amplitudes_A, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
histogram(amplitudes_B, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Channel A', 'Channel B');
xlabel('Peak Amplitude [V]');
ylabel('Counts');
title('Overlapped Histogram: Channel A & B');
grid on;
hold off;
clear;