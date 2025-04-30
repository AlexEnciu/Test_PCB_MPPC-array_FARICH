% Folder with .mat files
folder = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\Cosmics_hardware_coincidence';
files = dir(fullfile(folder, '*.mat'));

% Pre-allocate big arrays for all waveform values
all_values_A = [];
all_values_B = [];

% Loop over all files and concatenate all waveform samples
for i = 1:length(files)
    data = load(fullfile(folder, files(i).name));
    all_values_A = [all_values_A; data.A(:)];
    all_values_B = [all_values_B; data.B(:)];

    % Status update
    fprintf('Processed file %d of %d: %s\n', i, length(files), files(i).name);
end

% Histogram for Channel A (all values)
figure('Position', [100, 100, 1200, 600]);
histogram(all_values_A, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'EdgeColor', 'none');
xlabel('Amplitude [mV]');
ylabel('Counts');
title('Histogram of All Sample Values - Channel A');
grid on;

% Histogram for Channel B (all values)
figure('Position', [100, 100, 1200, 600]);
histogram(all_values_B, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'EdgeColor', 'none');
xlabel('Amplitude [mV]');
ylabel('Counts');
title('Histogram of All Sample Values - Channel B');
grid on;

% Overlapped Histogram
figure('Position', [100, 100, 1200, 600]);
hold on;
histogram(all_values_A, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
histogram(all_values_B, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Channel A', 'Channel B');
xlabel('Amplitude [mV]');
ylabel('Counts');
title('Overlapped Histogram: All Sample Values A & B');
grid on;
hold off;
