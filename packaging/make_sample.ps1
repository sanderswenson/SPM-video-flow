# CI helper: synthesize a short "raw take" with a filler word and a retake, as sample.mp4.
# Uses the built-in Windows speech synthesizer (run with Windows PowerShell, not pwsh).
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = 0
$s.SetOutputToWaveFile("$PWD\speech.wav")
$s.Speak("So, um, today I want to show you the best way to edit. Wait, let me redo that.")
$s.Speak("Today I want to show you the fastest way to edit your videos.")
$s.Dispose()
& packaging\ffmpeg\ffmpeg.exe -hide_banner -loglevel error -y -f lavfi -i "testsrc2=size=1920x1080:rate=30" `
  -i speech.wav -shortest -c:v libx264 -preset ultrafast -c:a aac sample.mp4
if ($LASTEXITCODE -ne 0) { exit 1 }
