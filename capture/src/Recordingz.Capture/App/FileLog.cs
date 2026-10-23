using System.Globalization;
using System.Security.AccessControl;
using System.Text;
using Recordingz.Capture.Core;

namespace Recordingz.Capture.App;

/// <summary>Appends to &lt;out_dir&gt;/recordingz.log in the prototype's format
/// ("%(asctime)s %(levelname)s %(message)s" = "2026-09-29 16:58:01,123 INFO message"), so the Python and the
/// .NET app can share one log file and the same grep habits keep working.</summary>
public sealed class FileLog : IDisposable
{
    public const long MaxBytes = 5 * 1024 * 1024;  // a larger log is moved to recordingz.log.1 at start

    private readonly object _lock = new();
    private readonly string _path;
    private StreamWriter? _writer;

    public FileLog(string path)
    {
        _path = path;
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        Rotate(path);
        Open();
    }

    /// <summary>Keeps the log from growing forever: over <paramref name="maxBytes"/> it becomes &lt;log&gt;.1 (the
    /// previous .1 is dropped). When the transcribe part has the file open, the move fails and waits for the
    /// next start.</summary>
    internal static void Rotate(string path, long maxBytes = MaxBytes)
    {
        try
        {
            var info = new FileInfo(path);
            if (info.Exists && info.Length > maxBytes)
                File.Move(path, path + ".1", overwrite: true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }

    private void Open()
    {
        // FileShare.ReadWrite | Delete: the screen capture process, the transcribe part (or an editor) may have the
        // same file open; a log must never be the reason the recorder does not start. Append-only access makes
        // Windows put every write at the current end, so writers in other processes do not overwrite each other.
        var fs = new FileInfo(_path).Create(FileMode.Append, FileSystemRights.AppendData,
                                            FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.None, null);
        _writer = new StreamWriter(fs, new UTF8Encoding(false)) { AutoFlush = true };
    }

    /// <summary>One log line, exactly like Python logging's default asctime + levelname.</summary>
    public static string Format(DateTime at, string level, string message) =>
        at.ToString("yyyy-MM-dd HH:mm:ss,fff", CultureInfo.InvariantCulture) + " " + level + " " + message;

    /// <summary>Log.Sink target: (level, message).</summary>
    public void Write(string level, string message)
    {
        var line = Format(DateTime.Now, level, message);
        lock (_lock)
        {
            try
            {
                if (_writer is null) Open();
                _writer!.WriteLine(line);
            }
            catch (IOException)
            {
                // the disk went away (a network drive, a full disk): drop the line rather than crash a recording
                try { _writer?.Dispose(); } catch (IOException) { }
                _writer = null;
            }
            catch (ObjectDisposedException)
            {
                _writer = null;
            }
        }
    }

    /// <summary>Python log.exception: the message plus the stack trace, at ERROR.</summary>
    public static void Exception(string message, Exception e) => Log.Error($"{message}\n{e}");

    public void Dispose()
    {
        lock (_lock)
        {
            _writer?.Dispose();
            _writer = null;
        }
    }
}
