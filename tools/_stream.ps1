param([string]$Ip="192.168.99.50",[int]$Port=5701,[int]$Seconds=0)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$sw=[System.Diagnostics.Stopwatch]::StartNew()
$scr=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$src=New-Object System.Drawing.Bitmap $scr.Width,$scr.Height
$sg=[System.Drawing.Graphics]::FromImage($src)
$dst=New-Object System.Drawing.Bitmap 640,480,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb
$dg=[System.Drawing.Graphics]::FromImage($dst)
$dg.InterpolationMode=[System.Drawing.Drawing2D.InterpolationMode]::Bilinear
$font=New-Object System.Drawing.Font "Consolas",16
$scale=[Math]::Min(640.0/$scr.Width,480.0/$scr.Height)
$w=[int]($scr.Width*$scale); $h=[int]($scr.Height*$scale)
$dx=[int]((640-$w)/2); $dy=[int]((480-$h)/2)
$destRect=New-Object System.Drawing.Rectangle $dx,$dy,$w,$h
$srcRect=New-Object System.Drawing.Rectangle 0,0,$scr.Width,$scr.Height
$tcp=New-Object System.Net.Sockets.TcpClient
$tcp.NoDelay=$true
$tcp.Connect($Ip,$Port)
$st=$tcp.GetStream()
$frames=0
while($true){
  $sg.CopyFromScreen($srcRect.Location,[System.Drawing.Point]::Empty,$srcRect.Size)
  $dg.FillRectangle([System.Drawing.Brushes]::Black,0,0,640,480)
  $dg.DrawImage($src,$destRect)
  $frames++
  $x=($frames*12)%600
  $dg.FillRectangle([System.Drawing.Brushes]::Lime,$x,462,60,16)
  $dg.DrawString(("f={0}  {1:HH:mm:ss.fff}" -f $frames,(Get-Date)),$font,[System.Drawing.Brushes]::Yellow,8,8)
  $bd=$dst.LockBits((New-Object System.Drawing.Rectangle 0,0,640,480),[System.Drawing.Imaging.ImageLockMode]::ReadOnly,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $bytes=New-Object byte[] (640*480*4)
  [System.Runtime.InteropServices.Marshal]::Copy($bd.Scan0,$bytes,0,$bytes.Length)
  $dst.UnlockBits($bd)
  $st.Write($bytes,0,$bytes.Length); $st.Flush()
  if($Seconds -gt 0 -and $sw.Elapsed.TotalSeconds -ge $Seconds){break}
  $sleep=($frames/15.0)-$sw.Elapsed.TotalSeconds
  if($sleep -gt 0){Start-Sleep -Milliseconds ([int]($sleep*1000))}
}
$tcp.Close()
Write-Output ("sent {0} frames in {1:N1}s = {2:N1} fps" -f $frames,$sw.Elapsed.TotalSeconds,($frames/$sw.Elapsed.TotalSeconds))