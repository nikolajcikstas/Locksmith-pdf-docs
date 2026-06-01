param(
  [Parameter(Mandatory=$true)]
  [string]$ImagePath
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

function Await-AsyncOperation {
  param(
    [Parameter(Mandatory=$true)]
    $Operation,
    [Parameter(Mandatory=$true)]
    [Type]$ResultType
  )

  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
      $_.Name -eq 'AsTask' -and
      $_.IsGenericMethodDefinition -and
      $_.GetParameters().Count -eq 1
    } |
    Select-Object -First 1

  $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.Wait()
  return $task.Result
}

$storageFileType = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$fileAccessModeType = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$randomAccessStreamType = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$bitmapDecoderType = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$softwareBitmapType = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$bitmapPixelFormatType = [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$bitmapAlphaModeType = [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$ocrEngineType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$ocrResultType = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

$fullPath = [System.IO.Path]::GetFullPath($ImagePath)
$file = Await-AsyncOperation ($storageFileType::GetFileFromPathAsync($fullPath)) $storageFileType
$stream = Await-AsyncOperation ($file.OpenAsync($fileAccessModeType::Read)) $randomAccessStreamType
$decoder = Await-AsyncOperation ($bitmapDecoderType::CreateAsync($stream)) $bitmapDecoderType
$bitmap = Await-AsyncOperation ($decoder.GetSoftwareBitmapAsync()) $softwareBitmapType
$bitmap = $softwareBitmapType::Convert($bitmap, $bitmapPixelFormatType::Bgra8, $bitmapAlphaModeType::Premultiplied)

$engine = $ocrEngineType::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
  throw "Windows OCR engine is not available for the current user languages."
}

$result = Await-AsyncOperation ($engine.RecognizeAsync($bitmap)) $ocrResultType
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Output $result.Text
