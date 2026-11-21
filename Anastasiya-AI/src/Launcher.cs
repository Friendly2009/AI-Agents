using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--service")
        {
            ServiceBase.Run(new AgentService());
            return;
        }
        try
        {
            if (!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator))
            {
                Process.Start(new ProcessStartInfo(Application.ExecutablePath) { Verb = "runas", UseShellExecute = true });
                return;
            }
            var assembly = Assembly.GetExecutingAssembly();
            var baseDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                "Anastasiya AI", "versions");
            Directory.CreateDirectory(baseDir);
            ProtectProfile(Path.Combine(Path.GetDirectoryName(baseDir), "profile"));
            var install = Path.Combine(baseDir, assembly.ManifestModule.ModuleVersionId.ToString("N"));
            if (!Directory.Exists(install))
            {
                var staging = Path.Combine(baseDir, Guid.NewGuid().ToString("N"));
                Directory.CreateDirectory(staging);
                try
                {
                    using (var stream = assembly.GetManifestResourceStream("agent.zip"))
                    using (var archive = new ZipArchive(stream))
                        archive.ExtractToDirectory(staging);
                    File.Copy(Application.ExecutablePath, Path.Combine(staging, "AnastasiyaAI.exe"));
                    try { Directory.Move(staging, install); }
                    catch (IOException) { if (!Directory.Exists(install)) throw; }
                }
                finally { if (Directory.Exists(staging)) Directory.Delete(staging, true); }
            }
            var start = new ProcessStartInfo(Path.Combine(install, "runtime", "pythonw.exe"),
                "-I \"" + Path.Combine(install, "desktop.py") + "\"");
            start.WorkingDirectory = install;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        }
        catch (Exception error)
        {
            MessageBox.Show("Не удалось открыть приложение. " + error.Message, "Анастасия AI");
        }
    }

    private static void ProtectProfile(string path)
    {
        var acl = new DirectorySecurity();
        acl.SetAccessRuleProtection(true, false);
        var inherit = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
        var name = Encoding.Unicode.GetBytes("ANASTASIYAAI");
        byte[] hash;
        using (var sha = SHA1.Create()) hash = sha.ComputeHash(name);
        var sid = "S-1-5-80";
        for (int i = 0; i < hash.Length; i += 4) sid += "-" + BitConverter.ToUInt32(hash, i);
        foreach (var principal in new[] { "S-1-5-18", "S-1-5-32-544", sid })
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(principal),
                principal == sid ? FileSystemRights.Modify : FileSystemRights.FullControl,
                inherit, PropagationFlags.None, AccessControlType.Allow));
        Directory.CreateDirectory(path, acl);
        Directory.SetAccessControl(path, acl);
    }
}

internal sealed class AgentService : ServiceBase
{
    private Process agent;
    private volatile bool stopping;
    internal AgentService() { ServiceName = "AnastasiyaAI"; CanShutdown = true; }
    protected override void OnStart(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        var start = new ProcessStartInfo(Path.Combine(root, "runtime", "python.exe"),
            "-I \"" + Path.Combine(root, "desktop.py") + "\" --worker");
        start.WorkingDirectory = root;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardInput = true;
        agent = new Process { StartInfo = start, EnableRaisingEvents = true };
        agent.Exited += delegate { if (!stopping) { ExitCode = 1067; Stop(); } };
        agent.Start();
    }
    protected override void OnStop()
    {
        stopping = true;
        if (agent != null && !agent.HasExited)
        {
            RequestAdditionalTime(20000);
            try { agent.StandardInput.Close(); } catch (IOException) {}
            if (!agent.WaitForExit(15000)) { agent.Kill(); agent.WaitForExit(3000); }
        }
    }
    protected override void OnShutdown() { OnStop(); }
}
