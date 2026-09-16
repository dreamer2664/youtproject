DROP YOUR OWN MUSIC HERE (optional)
====================================

youtproject ships with a built-in royalty-free music bed, so you can
ignore this folder completely and everything still works.

Want your own vibe instead? Two options:

  1. Drop audio files in this folder (.mp3, .wav, .ogg, .m4a...).
     Every video picks one at RANDOM, so a library = endless variety.
     The pick is logged to <video>.music.txt next to the mp4 (license record).
  2. Or point at any file explicitly in config.yaml:
       music:
         file: "C:/Music/my-track.mp3"

The track is looped to the video length and automatically ducked
under the narration, so any full-length song works.

WHERE TO GET FREE, MONETIZATION-SAFE MUSIC
------------------------------------------
YouTube Studio -> Audio Library (free, cleared for YouTube).
Check each track's license column: "attribution not required" is
zero-effort; "attribution required" means you paste the credit line
into your video description (description.txt in the upload kit).

This folder's audio files are git-ignored — they stay on your PC.

GETTING TRACKS FROM ICONS8 (student plan: 3 months — grab them NOW)
-------------------------------------------------------------------
1. Log in at icons8.com -> Music. Search moods that fit fast Shorts:
   "upbeat corporate", "curious ambient", "cinematic inspiring",
   "playful quirky", "dark suspense".
2. Download 15-20 tracks, MP3 320kbps (WAV if offered).
3. BEFORE each download: open its license page and confirm it covers
   YouTube COMMERCIAL/monetized use. If it needs attribution, save the
   credit line in a matching .txt (my-track.credit.txt) — you paste it
   into description.txt in the upload kit.
4. Name files clearly (genre-mood-01.mp3) and drop them here. Done —
   the next render rotates through them automatically.
