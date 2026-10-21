# Testing the DOT voice agent

Thanks for helping! The agent cold-calls colleges to pitch DOT. You'll play the person who
picks up, talking to it through your browser. It takes about 10 minutes to set up and 3 to 5
minutes per call.

## What you need

- A Windows PC, Linux PC, or an Apple Silicon Mac (M1 or newer, macOS 14+). Intel Macs aren't supported.
- Chrome, Edge or Safari, a microphone, and ideally headphones.
- The Sarvam API key Akhil sent you.

## 1. Start it

Unzip the folder (or `git clone` it), then:

- **Windows**: double-click `start.bat`. If Windows says "Windows protected your PC", click **More info → Run anyway**.
- **Mac**: right-click `start.command` → **Open** → **Open** (only the first time; after that a double-click works).
- **Linux**: run `bash start.command` in a terminal.

The first run downloads Python and the app's packages, which takes a few minutes. It then asks
for the Sarvam API key. Paste it and press Enter. You're only asked once.

The call page opens in your browser at <http://localhost:7860>. Leave the black terminal window open while you test.

## 2. Make some calls

1. Type your name in the box. It's saved with every call so we know who tested what.
2. Press **Start call** and allow the microphone.
3. The agent speaks first. Play one of these roles:
   - **College receptionist**: be busy, ask who's calling, maybe transfer, maybe refuse.
   - **Placement officer**: ask hard questions, then try to book a meeting.
   - **Dean or principal**: be short on time.
   - **Librarian**: try Hindi or Hinglish.
   - **Difficult**: interrupt, go off-topic, say "don't call again", go silent.
4. Hang up whenever you like, or let the agent end the call.

Please make **at least 5 calls** with different roles. Short, weird and failed calls are all useful.

If something goes wrong (it didn't hear you, a long pause, it said something odd), write down
the **call reference** shown under the call box and a line about what happened.

## 3. Send the results back

When you're done, click the terminal window and press **Ctrl+C**. It saves a file like
`dot-test-results-Priya-20261021-1830.zip` in the folder and prints where it is.

Send that zip to Akhil (by Drive, WeTransfer or WhatsApp document), along with your notes.

If you closed the window instead, double-click `export-results.bat` (Windows) or
`export-results.command` (Mac/Linux) to make the zip.

**What's in the zip:** your call recordings, transcripts, the agent's results and timings, and
the app's logs. Your API key is removed from the logs.

## Problems?

- **"Address already in use"**: the agent is already running in another window. Close that one first.
- **No sound or no mic**: check the browser's microphone permission for `localhost` and use headphones.
- **Anything else**: send a screenshot of the terminal window to Akhil.
