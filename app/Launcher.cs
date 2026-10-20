using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("Telemetry")]
[assembly: AssemblyProduct("Telemetry")]
[assembly: AssemblyDescription("Telemetry hardware dashboard launcher")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(directory, "launch-dashboard.ps1");
        if (!File.Exists(script))
        {
            return ShowError("Keep Telemetry.exe beside launch-dashboard.ps1 in the dashboard project folder.");
        }

        bool stop = Array.Exists(args, value => value.Equals("-Stop", StringComparison.OrdinalIgnoreCase));
        string key;
        using (SHA256 hash = SHA256.Create())
            key = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(directory.ToUpperInvariant()))).Replace("-", "");
        string jobName = @"Local\TelemetryJob-" + key;
        using (Mutex gate = new Mutex(false, @"Local\TelemetrySession-" + key))
        {
            bool acquired = false;
            try
            {
                try { acquired = gate.WaitOne(0); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired)
                {
                    // The previous supervisor owns the job and releases the gate after cleanup.
                    DateTime deadline = DateTime.UtcNow.AddSeconds(20);
                    while (!acquired && DateTime.UtcNow < deadline)
                    {
                        IntPtr previous = OpenJobObject(0x0008, false, jobName);
                        if (previous != IntPtr.Zero)
                        {
                            try { if (!TerminateJobObject(previous, 0)) throw new Win32Exception(); }
                            finally { CloseHandle(previous); }
                        }
                        try { acquired = gate.WaitOne(200); }
                        catch (AbandonedMutexException) { acquired = true; }
                    }
                    if (!acquired) throw new InvalidOperationException("The previous dashboard session did not stop within 20 seconds.");
                }
                if (stop) return 0;
                return RunSession(directory, script, args, jobName);
            }
            catch (Win32Exception error)
            {
                if (acquired) { gate.ReleaseMutex(); acquired = false; }
                return ShowError(error.Message);
            }
            catch (InvalidOperationException error)
            {
                if (acquired) { gate.ReleaseMutex(); acquired = false; }
                return ShowError(error.Message);
            }
            finally { if (acquired) gate.ReleaseMutex(); }
        }
    }

    private static int RunSession(string directory, string script, string[] args, string jobName)
    {
        string powershell = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe");
        StringBuilder arguments = new StringBuilder(
            "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ");
        arguments.Append(Quote(script));
        foreach (string argument in args)
        {
            arguments.Append(' ').Append(Quote(argument));
        }
        arguments.Append(" -ManagedSession");

        IntPtr job = IntPtr.Zero;
        DateTime deadline = DateTime.UtcNow.AddSeconds(15);
        do
        {
            job = CreateJobObject(IntPtr.Zero, jobName);
            if (job == IntPtr.Zero) throw new Win32Exception();
            if (Marshal.GetLastWin32Error() != 183) break;
            CloseHandle(job);
            job = IntPtr.Zero;
            Thread.Sleep(100);
        } while (DateTime.UtcNow < deadline);
        if (job == IntPtr.Zero) throw new InvalidOperationException("The previous dashboard process tree is still shutting down.");
        PROCESS_INFORMATION child = new PROCESS_INFORMATION();
        try
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x2000; // Kill every descendant when the supervisor closes.
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(limits)))
                throw new Win32Exception();
            STARTUPINFO startup = new STARTUPINFO();
            startup.cb = Marshal.SizeOf(startup);
            StringBuilder command = new StringBuilder(Quote(powershell) + " " + arguments);
            // Suspend before assignment so no child can escape the job during startup.
            if (!CreateProcess(powershell, command, IntPtr.Zero, IntPtr.Zero, false,
                0x08000004, IntPtr.Zero, directory, ref startup, out child))
                throw new Win32Exception();
            if (!AssignProcessToJobObject(job, child.hProcess))
            {
                int error = Marshal.GetLastWin32Error();
                TerminateProcess(child.hProcess, 1);
                throw new Win32Exception(error);
            }
            if (ResumeThread(child.hThread) == uint.MaxValue) throw new Win32Exception();
            if (WaitForSingleObject(child.hProcess, uint.MaxValue) != 0) throw new Win32Exception();
            uint exitCode;
            if (!GetExitCodeProcess(child.hProcess, out exitCode)) throw new Win32Exception();
            return unchecked((int)exitCode);
        }
        finally
        {
            CloseHandle(job);
            if (child.hThread != IntPtr.Zero) CloseHandle(child.hThread);
            if (child.hProcess != IntPtr.Zero) CloseHandle(child.hProcess);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public uint dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public uint dwProcessId, dwThreadId;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string application, StringBuilder command, IntPtr processAttributes,
        IntPtr threadAttributes, bool inherit, uint flags, IntPtr environment, string directory,
        ref STARTUPINFO startup, out PROCESS_INFORMATION process);

    // Preserve spaces, quotes, and trailing backslashes in Windows process arguments.
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            result.Append('\\', character == '"' ? backslashes * 2 + 1 : backslashes);
            result.Append(character);
            backslashes = 0;
        }
        result.Append('\\', backslashes * 2).Append('"');
        return result.ToString();
    }

    private static int ShowError(string message)
    {
        Console.Error.WriteLine(message);
        string data = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "data");
        Directory.CreateDirectory(data);
        File.AppendAllText(Path.Combine(data, "dashboard.supervisor.log"),
            DateTime.Now.ToString("O") + " " + message + Environment.NewLine);
        MessageBox.Show(message, "Telemetry", MessageBoxButtons.OK, MessageBoxIcon.Error);
        return 1;
    }
}
