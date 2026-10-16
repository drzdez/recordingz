using System.Diagnostics;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Recording;

/// <summary>The mono 16 kHz mix for cloud ASR (contract: mix), made by ffmpeg when it is installed.</summary>
public static class Mixer
{
    /// <summary>Mixing is optional: without ffmpeg the recording keeps its separate tracks and no `mix`.</summary>
    public const bool MixWithFfmpeg = true;

    public const int MixSampleRate = 16000;
    public const int MixChannels = 1;

    /// <summary>ffmpeg.exe: in RECORDINGZ_FFMPEG_DIR, the one the suite MSI brings, where `winget install
    /// Gyan.FFmpeg` puts it (winget does not always add it to PATH for already running processes), else on PATH.
    /// The known places come first: an old or broken ffmpeg somewhere on PATH must not win over them. Null when
    /// none exists.</summary>
    public static string? FindFfmpeg() =>
        FindFfmpeg(Environment.GetEnvironmentVariable("RECORDINGZ_FFMPEG_DIR"), AppContext.BaseDirectory,
                   Environment.GetEnvironmentVariable("LOCALAPPDATA"), Environment.GetEnvironmentVariable("PATH"));

    internal static string? FindFfmpeg(string? envDir, string appDir, string? localAppData, string? path)
    {
        var cands = new List<string>();
        if (!string.IsNullOrEmpty(envDir))
            cands.Add(Path.Combine(envDir, "ffmpeg.exe"));
        cands.Add(Path.Combine(appDir, "ffmpeg", "ffmpeg.exe"));  // the suite MSI brings its own
        var local = localAppData ?? "";
        var packages = Path.Combine(local, "Microsoft", "WinGet", "Packages");
        try
        {
            if (local.Length > 0 && Directory.Exists(packages))
                foreach (var pkg in Directory.EnumerateDirectories(packages, "Gyan.FFmpeg_*").Order())
                    foreach (var ver in Directory.EnumerateDirectories(pkg, "ffmpeg-*").Order())
                        cands.Add(Path.Combine(ver, "bin", "ffmpeg.exe"));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
        foreach (var dir in (path ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                cands.Add(Path.Combine(dir.Trim().Trim('"'), "ffmpeg.exe"));
            }
            catch (ArgumentException) { }  // a malformed PATH entry must not break the lookup
        }
        return cands.FirstOrDefault(File.Exists);
    }

    /// <summary>&lt;stem&gt;_mix.wav: all tracks summed (amix, no normalisation so quiet tracks stay audible
    /// relative to each other), mono, 16 kHz PCM. Returns its path, or null when ffmpeg failed.</summary>
    public static string? Mix(string ffmpeg, string stemPath, IList<string> wavs)
    {
        if (wavs.Count == 0)
            return null;
        var mix = stemPath + "_mix.wav";
        var psi = new ProcessStartInfo(ffmpeg)
        {
            UseShellExecute = false,
            CreateNoWindow = true,  // a console flashing up at the end of every call is not acceptable
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        };
        foreach (var a in new[] { "-y", "-loglevel", "error" })
            psi.ArgumentList.Add(a);
        foreach (var w in wavs)
        {
            psi.ArgumentList.Add("-i");
            psi.ArgumentList.Add(w);
        }
        foreach (var a in new[] { "-filter_complex", $"amix=inputs={wavs.Count}:duration=longest:normalize=0",
                                  "-ac", "1", "-ar", "16000", mix })
            psi.ArgumentList.Add(a);
        try
        {
            using var p = Process.Start(psi);
            if (p is null)
            {
                Log.Error("ffmpeg: could not start " + ffmpeg);
                return null;
            }
            // Read both pipes asynchronously: a full stderr pipe would block ffmpeg forever.
            var err = p.StandardError.ReadToEndAsync();
            var outp = p.StandardOutput.ReadToEndAsync();
            p.WaitForExit();
            if (p.ExitCode != 0)
            {
                Log.Error("ffmpeg: " + err.Result);
                return null;
            }
            _ = outp.Result;
            return mix;
        }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or IOException or InvalidOperationException)
        {
            Log.Error("ffmpeg: " + e.Message);
            return null;
        }
    }
}
