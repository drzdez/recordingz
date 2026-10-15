using Microsoft.Win32;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.Config;

/// <summary>
/// recordingz was teamsrec until 2.0. On the first start after the update the app takes over what teamsrec 1.x
/// kept: the shared config (%APPDATA%\teamsrec\teamsrec.toml) and its registry values (HKCU\Software\teamsrec\capture:
/// the recordings folder for the next installer, the update state). Copies only while recordingz has none of its own;
/// the old ones stay as they were.
/// </summary>
public static class LegacyMigration
{
    public const string LegacyKey = @"Software\teamsrec\capture";

    /// <summary>%APPDATA%\teamsrec\teamsrec.toml.</summary>
    public static string LegacyConfigPath =>
        Path.Combine(Environment.GetEnvironmentVariable("APPDATA") ?? "", "teamsrec", "teamsrec.toml");

    /// <summary>Copy <paramref name="legacy"/> to <paramref name="path"/> unless there is one already.</summary>
    public static bool CopyConfig(string path, string legacy)
    {
        if (File.Exists(path) || !File.Exists(legacy))
            return false;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.Copy(legacy, path);
        return true;
    }

    /// <summary>Copy the values of HKCU\<paramref name="from"/> to HKCU\<paramref name="to"/> unless that exists.</summary>
    public static bool CopyRegistry(string from, string to)
    {
        using var existing = Registry.CurrentUser.OpenSubKey(to);
        if (existing is not null)
            return false;
        using var old = Registry.CurrentUser.OpenSubKey(from);
        if (old is null)
            return false;
        using var key = Registry.CurrentUser.CreateSubKey(to);
        foreach (var name in old.GetValueNames())
            if (old.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames) is { } value)
                key.SetValue(name, value, old.GetValueKind(name));
        return true;
    }

    /// <summary>A line for the log: (INFO | WARNING, text).</summary>
    public readonly record struct Note(string Level, string Text);

    /// <summary>Both, against the real places (the config only without RECORDINGZ_CONFIG).</summary>
    public static IReadOnlyList<Note> Run() =>
        Run(string.IsNullOrEmpty(Environment.GetEnvironmentVariable("RECORDINGZ_CONFIG")) ? AppConfig.DefaultPath : null,
            LegacyConfigPath, LegacyKey, InstallerFolder.Key);

    /// <summary>Both; what was taken over is returned, not logged: it runs before the log is open (the log lives in
    /// the recordings folder, which this config names). A problem never stops the app.</summary>
    public static IReadOnlyList<Note> Run(string? path, string legacy, string fromKey, string toKey)
    {
        var notes = new List<Note>();
        try
        {
            if (path is not null && CopyConfig(path, legacy))
                notes.Add(new("INFO", $"config of teamsrec 1.x copied from {legacy}"));
            if (CopyRegistry(fromKey, toKey))
                notes.Add(new("INFO", $@"registry values of teamsrec 1.x copied from HKCU\{fromKey}"));
        }
        catch (Exception e)
        {
            notes.Add(new("WARNING", $"teamsrec 1.x settings not taken over: {e.Message}"));
        }
        return notes;
    }
}
