using System.Text.Json;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.App;

/// <summary>The review page's Nápověda → O aplikaci asks this app to look for a new version or to install it: the
/// review server drops <c>%TEMP%\recordingz-update.request</c> with "check" or "install", and reads the answer from
/// <c>%TEMP%\recordingz-update.json</c>. Files, like the capture status: the app has no server of its own.</summary>
public static class UpdateRequest
{
    public const string FileName = "recordingz-update.request";
    public const string StateFileName = "recordingz-update.json";
    public static readonly string[] Actions = ["check", "install"];

    public static string StatePath => Path.Combine(Path.GetTempPath(), StateFileName);

    /// <summary>Watches <paramref name="dir"/> for the request file and hands its action over (unknown text: none).
    /// A stale request is deleted first, so a start never installs anything by itself. Null when it cannot watch.</summary>
    public static FileSystemWatcher? Watch(string dir, Action<string> onRequest)
    {
        var path = Path.Combine(dir, FileName);
        TryDelete(path);
        try
        {
            var w = new FileSystemWatcher(dir, FileName) { NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite };
            FileSystemEventHandler handler = (_, _) =>
            {
                var action = Read(path);
                TryDelete(path);
                if (action is not null)
                    onRequest(action);
            };
            w.Created += handler;
            w.Changed += handler;
            w.EnableRaisingEvents = true;
            return w;
        }
        catch (Exception e)
        {
            Log.Warn($"cannot watch {dir} for the review page's update request: {e.Message}");
            return null;
        }
    }

    /// <summary>The request's action, null when the file is gone (a second event of the same write) or says nothing
    /// known. Read with a few retries: the writer may still hold the file.</summary>
    private static string? Read(string path)
    {
        for (int i = 0; i < 5; i++)
        {
            try
            {
                var text = File.ReadAllText(path).Trim().ToLowerInvariant();
                return Actions.Contains(text) ? text : null;
            }
            catch (FileNotFoundException) { return null; }
            catch (IOException) { Thread.Sleep(50); }
            catch (UnauthorizedAccessException) { Thread.Sleep(50); }
        }
        return null;
    }

    /// <summary>The answer for the page: state = checking | current | available | failed | installing, the running
    /// version, the newer one when there is one, and a message.</summary>
    public static string StateJson(string state, string current, string? available, string message, DateTime at) =>
        JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["state"] = state, ["current"] = current, ["available"] = available, ["message"] = message,
            // to the millisecond: two answers within one second must still differ for the page
            ["at"] = at.ToString("yyyy-MM-ddTHH:mm:ss.fff"), ["pid"] = Environment.ProcessId,
        });

    /// <summary>Writes the answer; never fails the caller (the page then simply waits and says it got none).</summary>
    public static void WriteState(string state, string? available, string message, string? path = null)
    {
        path ??= StatePath;
        try
        {
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, StateJson(state, Versions.AppVersion, available, message, DateTime.Now));
            File.Move(tmp, path, overwrite: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            Log.Warn($"update state not written: {e.Message}");
        }
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
