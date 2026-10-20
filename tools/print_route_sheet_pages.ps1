param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$Receipt,
    [string]$Printer = 'Kyocera 5',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$taskManifest = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
foreach ($page in $taskManifest.pages) {
    if ((Get-FileHash -LiteralPath $page.path -Algorithm SHA256).Hash.ToLower() -ne $page.sha256) {
        throw "Page hash mismatch: $($page.path)"
    }
}
if (!$taskManifest.pages.Count) { throw 'No verified pages' }
$taskPrinter = Get-Printer -Name $Printer
if ($taskPrinter.PrinterStatus -ne 'Normal') { throw "Printer status: $($taskPrinter.PrinterStatus)" }
if (Test-Path -LiteralPath $Receipt) {
    $prior = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    if ($prior.batch_id -ne $taskManifest.batch_id) { throw 'Receipt belongs to another batch' }
    if ($prior.status -eq 'spooled') { $prior | ConvertTo-Json; exit 0 }
    throw 'Prior submission is uncertain; inspect the spool job before any retry'
}
if ($DryRun) {
    @{printer=$Printer;pages=$taskManifest.pages.Count;duplex='Simplex';batch_id=$taskManifest.batch_id} | ConvertTo-Json
    exit 0
}
$receiptLock = [System.IO.File]::Open($Receipt + '.lock', 'CreateNew', 'Write', 'None')
try {
    Add-Type -AssemblyName System.Drawing
    $driverRefs = @(
        [System.Drawing.Printing.PrinterSettings].Assembly.Location,
        [System.Drawing.Point].Assembly.Location,
        [System.ComponentModel.Component].Assembly.Location
    ) + @(Get-ChildItem -LiteralPath $PSHOME -Filter 'System.Private.Windows.*.dll' | ForEach-Object FullName)
    Add-Type -ReferencedAssemblies $driverRefs -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Printing;
using System.Runtime.InteropServices;
public static class VerifiedRoutePrinter {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public class DocInfo { public int size=Marshal.SizeOf(typeof(DocInfo)); public string name; public string output=null; public string type=null; public int flags=0; }
  [DllImport("gdi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateDC(string driver,string device,string output,IntPtr mode);
  [DllImport("gdi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern int StartDoc(IntPtr dc,DocInfo info);
  [DllImport("gdi32.dll")] static extern int StartPage(IntPtr dc);
  [DllImport("gdi32.dll")] static extern int EndPage(IntPtr dc);
  [DllImport("gdi32.dll")] static extern int EndDoc(IntPtr dc);
  [DllImport("gdi32.dll")] static extern int AbortDoc(IntPtr dc);
  [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
  [DllImport("gdi32.dll")] static extern int GetDeviceCaps(IntPtr dc,int cap);
  [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr handle);
  [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr handle);
  public static int Print(string printer,string title,string[] paths) {
    var settings=new PrinterSettings { PrinterName=printer, Duplex=Duplex.Simplex, Copies=1 };
    if(!settings.IsValid) throw new Exception("Invalid printer");
    var pageSettings=new PageSettings(settings) { Landscape=false };
    foreach(PaperSize paper in settings.PaperSizes) if(paper.Kind==PaperKind.A4) { pageSettings.PaperSize=paper; break; }
    IntPtr handle=settings.GetHdevmode(pageSettings), mode=GlobalLock(handle), dc=IntPtr.Zero;
    bool started=false;
    try {
      dc=CreateDC("WINSPOOL",printer,null,mode);
      if(dc==IntPtr.Zero) throw new Exception("CreateDC failed");
      int job=StartDoc(dc,new DocInfo { name=title });
      if(job<=0) throw new Exception("StartDoc failed");
      started=true;
      foreach(string path in paths) {
        if(StartPage(dc)<=0) throw new Exception("StartPage failed");
        using(var bitmap=Image.FromFile(path)) using(var graphics=Graphics.FromHdc(dc)) {
          graphics.PageUnit=GraphicsUnit.Pixel;
          int width=GetDeviceCaps(dc,8), height=GetDeviceCaps(dc,10);
          float scale=Math.Min((float)width/bitmap.Width,(float)height/bitmap.Height);
          graphics.DrawImage(bitmap,(width-bitmap.Width*scale)/2,(height-bitmap.Height*scale)/2,bitmap.Width*scale,bitmap.Height*scale);
        }
        if(EndPage(dc)<=0) throw new Exception("EndPage failed");
      }
      if(EndDoc(dc)<=0) throw new Exception("EndDoc failed");
      started=false;
      return job;
    } finally {
      if(started) AbortDoc(dc);
      if(dc!=IntPtr.Zero) DeleteDC(dc);
      GlobalUnlock(handle);
      Marshal.FreeHGlobal(handle);
    }
  }
}
'@
    $taskReceipt = @{batch_id=$taskManifest.batch_id;printer=$Printer;status='submitting';started_at=(Get-Date).ToString('o');pages=$taskManifest.pages.Count}
    $taskReceipt | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding utf8
    $paths = [string[]]@($taskManifest.pages | ForEach-Object {$_.path})
    $jobId = [VerifiedRoutePrinter]::Print($Printer, $taskManifest.batch_id, $paths)
    $taskReceipt.status = 'spooled'
    $taskReceipt.job_id = $jobId
    $taskReceipt.finished_at = (Get-Date).ToString('o')
    $taskReceipt | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding utf8
    $taskReceipt | ConvertTo-Json
} finally {
    $receiptLock.Dispose()
    Remove-Item -LiteralPath ($Receipt + '.lock')
}
