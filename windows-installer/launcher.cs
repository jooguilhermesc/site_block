// O Siteblock-Instalador.exe. Ele leva dentro o siteblock.py, o instalar.cmd e o logo (o build.ps1
// os embute). Ao abrir, grava os três numa pasta temporária e roda o instalar.cmd, que abre o assistente.
// Ele não pede a permissão de administrador: quem pede é o assistente, que precisa abrir na conta de quem
// vai usar o Siteblock.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Instalador do Siteblock")]
[assembly: AssemblyProduct("Siteblock")]

static class Launcher
{
    [STAThread]
    static int Main()
    {
        try
        {
            // A pasta fica lá no fim: o assistente continua usando os arquivos depois que este programa fecha.
            string folder = Path.Combine(Path.GetTempPath(), "Siteblock-instalador");
            Directory.CreateDirectory(folder);
            Assembly assembly = Assembly.GetExecutingAssembly();
            foreach (string name in assembly.GetManifestResourceNames())
            {
                using (Stream source = assembly.GetManifestResourceStream(name))
                using (FileStream target = File.Create(Path.Combine(folder, name)))
                    source.CopyTo(target);
            }
            Process.Start(new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                Arguments = "/c \"\"" + Path.Combine(folder, "instalar.cmd") + "\"\"",
                WorkingDirectory = folder,
                UseShellExecute = false,
            });
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "Não consegui abrir a instalação do Siteblock:\n\n" + error.Message,
                "Instalar o Siteblock", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
