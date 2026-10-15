using Microsoft.Win32;
using Recordingz.Capture.Config;

namespace Recordingz.Capture.Tests;

/// <summary>recordingz was teamsrec until 2.0: its config and registry values are taken over once.</summary>
public sealed class LegacyMigrationTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("recordingz-legacy-").FullName;
    private readonly string _root = $@"Software\recordingz-tests\{Guid.NewGuid():N}";

    public void Dispose()
    {
        Directory.Delete(_dir, true);
        Registry.CurrentUser.DeleteSubKeyTree(_root, false);
        using var parent = Registry.CurrentUser.OpenSubKey(@"Software\recordingz-tests");
        if (parent is { SubKeyCount: 0 })
            Registry.CurrentUser.DeleteSubKey(@"Software\recordingz-tests", false);
    }

    [Fact]
    public void The_old_config_is_copied_once_and_kept()
    {
        var legacy = Path.Combine(_dir, "teamsrec", "teamsrec.toml");
        var path = Path.Combine(_dir, "recordingz", "recordingz.toml");
        Assert.False(LegacyMigration.CopyConfig(path, legacy), "nothing to copy");
        Directory.CreateDirectory(Path.GetDirectoryName(legacy)!);
        File.WriteAllText(legacy, "[recordings]\nout_dir = \"D:/meetings\"\n");
        Assert.True(LegacyMigration.CopyConfig(path, legacy));
        Assert.Equal(File.ReadAllText(legacy), File.ReadAllText(path));
        Assert.True(File.Exists(legacy), "a copy: the old file stays");
        File.WriteAllText(path, "[recordings]\nout_dir = \"E:/x\"\n");
        Assert.False(LegacyMigration.CopyConfig(path, legacy), "only while recordingz has none");
        Assert.Contains("E:/x", File.ReadAllText(path));
    }

    [Fact]
    public void The_old_registry_values_are_copied_once()
    {
        string from = _root + @"\teamsrec", to = _root + @"\recordingz";
        Assert.False(LegacyMigration.CopyRegistry(from, to), "no old key");
        using (var old = Registry.CurrentUser.CreateSubKey(from))
        {
            old.SetValue("OutDirCurrent", @"D:\meetings");
            old.SetValue("ReviewShortcut", 1, RegistryValueKind.DWord);
        }
        Assert.True(LegacyMigration.CopyRegistry(from, to));
        using (var key = Registry.CurrentUser.OpenSubKey(to)!)
        {
            Assert.Equal(@"D:\meetings", key.GetValue("OutDirCurrent"));
            Assert.Equal(RegistryValueKind.DWord, key.GetValueKind("ReviewShortcut"));
        }
        Assert.False(LegacyMigration.CopyRegistry(from, to), "recordingz's own key exists now");
    }
}
