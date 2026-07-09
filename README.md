github https://github.com/silverdolphin863/claude-speak.git

python claude-speak.py

python cc-speak.py [options] [file]

# Read a file aloud
python cc-speak.py output.txt

# Pipe text
echo "Hello world" | python cc-speak.py

# Preview cleaned text without speaking
python cc-speak.py --preview "Some **markdown** with `code`"

# Real-time file monitoring
python cc-speak.py --follow /tmp/claude.log

# Use OpenAI TTS instead (requires OPENAI_API_KEY)
python cc-speak.py --backend openai --voice coral output.txt

Options:
  --follow, -f FILE    Watch file for new content (real-time mode)
  --backend, -b NAME   TTS backend: "edge" (free) or "openai" (paid)
  --voice, -v NAME     Voice name
  --rate, -r RATE      Edge-tts rate adjustment (e.g. "+20%")
  --speed, -s FLOAT    OpenAI speed multiplier (0.25-4.0)
  --output, -o FILE    Save audio to file instead of playing
  --keep-code          Don't strip code blocks
  --keep-paths         Don't strip file paths
  --raw                Skip all text cleaning
  --preview            Print cleaned text instead of speaking
  --debounce, -d MS    Debounce delay in follow mode (default: 2000)


python configure.py [options]

Options:
  --port, -p PORT      Server port (default: 8910)
  --no-browser         Don't auto-open browser

# fire and forget
bash -c "~/code/AI/audio/claude-speak/.venv/bin/python ~/code/AI/audio/claude-speak/speak_daemon.py > /dev/null 2>&1 &"
pgrep -fa "speak_daemon.py"
pkill -f "speak_daemon.py"

~/.config/systemd/user/claude-speak.service
systemctl --user status claude-speak

# UI
.venv/bin/python speak_ui.py
