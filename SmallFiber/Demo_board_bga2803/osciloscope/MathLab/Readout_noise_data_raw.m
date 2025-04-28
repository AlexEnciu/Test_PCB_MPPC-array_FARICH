% ---------- Configuration ----------
folder_data  = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\uranium_238';
folder_noise = 'C:\Users\alex_\Documents\IKP\HYDRA\Test_PCB_MPPC-array_FARICH\SmallFiber\Demo_board_bga2803\osciloscope\noise-0004';
samples_per_waveform = 2508;  % Update if different

% ---------- File Lists ----------
files_data  = dir(fullfile(folder_data, '*.mat'));
files_noise = dir(fullfile(folder_noise, '*.mat'));
n_data  = length(files_data);
n_noise = length(files_noise);

% ---------- Preallocate ----------
values_A_data  = zeros(n_data  * samples_per_waveform, 1);
values_B_data  = zeros(n_data  * samples_per_waveform, 1);
values_A_noise = zeros(n_noise * samples_per_waveform, 1);
values_B_noise = zeros(n_noise * samples_per_waveform, 1);

% ---------- Load Data Set ----------
for i = 1:n_data
    d = load(fullfile(folder_data, files_data(i).name));
    idx = (i - 1) * samples_per_waveform + 1;
    values_A_data(idx:idx+samples_per_waveform-1) = d.A(:);
    values_B_data(idx:idx+samples_per_waveform-1) = d.B(:);
    if mod(i, 1000) == 0
        fprintf('[DATA] Processed %d of %d files\n', i, n_data);
    end
end

% ---------- Load Noise Set ----------
for i = 1:n_noise
    d = load(fullfile(folder_noise, files_noise(i).name));
    idx = (i - 1) * samples_per_waveform + 1;
    values_A_noise(idx:idx+samples_per_waveform-1) = d.A(:);
    values_B_noise(idx:idx+samples_per_waveform-1) = d.B(:);
    if mod(i, 1000) == 0
        fprintf('[NOISE] Processed %d of %d files\n', i, n_noise);
    end
end

% ---------- Plot Channel A ----------
figure('Position', [100, 100, 1200, 600]);
hold on;
histogram(values_A_noise, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'k', ...
    'FaceAlpha', 0.4, ...
    'EdgeColor', 'none');
histogram(values_A_data, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'b', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Noise - Channel A', 'Data - Channel A');
xlabel('Sample Value [V]');
ylabel('Counts');
title('Overlapped Histogram: All Samples - Channel A');
grid on;
hold off;

% ---------- Plot Channel B ----------
figure('Position', [100, 100, 1200, 600]);
hold on;
histogram(values_B_noise, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'k', ...
    'FaceAlpha', 0.4, ...
    'EdgeColor', 'none');
histogram(values_B_data, 256, ...
    'DisplayStyle', 'bar', ...
    'FaceColor', 'r', ...
    'FaceAlpha', 0.5, ...
    'EdgeColor', 'none');
legend('Noise - Channel B', 'Data - Channel B');
xlabel('Sample Value [V]');
ylabel('Counts');
title('Overlapped Histogram: All Samples - Channel B');
grid on;
hold off;
