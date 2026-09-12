# UPLOAD-GUIDE — uploading manually in YouTube Studio

This is the full walkthrough. Each video's kit (`upload/<id>/`) also contains a
shorter `CHECKLIST.md` prefilled with that video's values — this guide explains
the *why* behind each step.

Time per video: about 3 minutes once you've done it twice.

---

## 1. Before your first upload (one-time, ~10 minutes)

1. **Create the channel** at <https://youtube.com/channel_switcher> if you
   haven't. Pick the name, handle and profile picture now — changing them later
   confuses the algorithm and your viewers.
2. **Verify your phone number** at <https://youtube.com/verify>. Free, instant,
   and required for custom thumbnails. Without it your `thumbnail.jpg` can't be
   used and you're stuck with auto-generated frames.
3. **Enable 2-Step Verification** on the Google account. Channels get stolen
   constantly; this is the cheapest insurance there is.

## 2. Upload the file

1. Open <https://youtube.com/upload> while logged into the channel.
2. Drag in `video.mp4` from the kit folder.
3. **Do not close the tab** while it uploads and processes. Fill in the fields
   below meanwhile — everything is copy-paste from the kit's `.txt` files.

## 3. Details

- **Title** — paste from `title.txt`. Already ≤ 100 characters. Read it once
  out loud; fix anything that sounds generated. A 30-second human edit here is
  worth more than any tag trick.
- **Description** — paste from `description.txt`. It ends with an AI-disclosure
  footer. **Do not delete that footer.**
- **Thumbnail** — *Upload File* → pick `thumbnail.jpg`. Custom thumbnails
  massively outperform auto frames; always upload it. (For Shorts, see
  section 8 — the thumbnail file is a fallback; viewers mostly see a
  freeze-frame you pick in the mobile app.)
- **Tags** — under *Show more*, paste from `tags.txt`. Already ≤ 500
  characters total. Tags barely matter for discovery in 2026, but they cost
  nothing.
- **Language** — set your narration language (prefilled in the checklist).
  This drives captions and search matching; don't leave it on auto.
- **Category** — set it (prefilled in the checklist). Wrong category buries
  videos in the wrong recommendations.

## 4. Altered content — REQUIRED, do not skip

YouTube shows this question for every upload:

> *Is this content altered or synthetic, and does it seem real?*

For videos made with this tool the answer is **Yes**. Then tick:

> *Generates realistic scenes that didn't happen.*

What happens if you skip it: YouTube can remove the video or restrict the
channel, and viewers increasingly filter for disclosed content anyway. The
description footer and this checkbox together are the complete, correct
disclosure. It takes five seconds.

## 5. Captions — upload the .srt even though subs are burned in

Subtitles are already burned into the picture, but still upload
`captions.srt`: Studio → left menu **Subtitles** → pick the video → *Add* →
*Upload file*. Why:

- Viewers can turn captions **off** (burned-in can't be disabled).
- YouTube **indexes caption text for search**.
- Auto-translate to other languages only works from uploaded captions.

If your video was made with `--no-subs`, there is no `.srt` — either let
YouTube auto-generate captions (slower, no Studio action needed) or regenerate
with subtitles on.

## 6. Audience

> *Is this video made for kids?*

Answer **No** unless the video genuinely targets children. Answering Yes
(disabling comments, notifications and most ads) or answering wrong in either
direction brings COPPA trouble you do not want. Documentary-style narration =
not for kids.

## 7. Visibility — schedule, don't dump

- **First video:** Public, publish now. Check it renders correctly (thumbnail,
  captions, end screen).
- **After that:** *Schedule* one video per day at a consistent hour. Your
  audience and the algorithm both prefer rhythm over floods.
- New channels should **not** publish 10 videos on day one. It looks like spam
  to automated systems and splits your tiny initial audience across videos
  instead of concentrating watch time on one.

## 8. Shorts (portrait videos)

- Anything vertical under 3 minutes is shelved as a Short **automatically** —
  there is no separate upload flow. Just upload normally.
- **Thumbnail:** Shorts show a freeze-frame, picked in the **mobile** YouTube /
  Studio app after upload (web Studio can't change it). Scroll to a frame with
  a clean background and visible subtitles.
- **Pacing:** the first 2 seconds decide everything. Watch your Short once and
  check the hook lands immediately — if the opening line is slow, regenerate
  with a punchier `--topic` angle.
- **Subtitles** are placed high enough to clear the Shorts UI overlay (title,
  like/subscribe buttons). Don't move them.
- Don't hashtag-stuff: `#Shorts` in the description is harmless but no longer
  required for shelfing.

## 9. After publishing

```bash
python main.py published <id> https://youtu.be/PASTE-ID-HERE
```

This marks the job done in the queue so `python main.py queue` stays truthful.

---

## Things that get AI channels in trouble

1. **Undisclosed AI content.** Covered above. Checkbox + footer, every video.
2. **Repetitious / mass-produced content.** YouTube's spam policy explicitly
   covers bulk-generated videos with little variation. Defenses: a genuinely
   specific niche, varied topics per video (`--topic`), human review of every
   script before upload, and one-a-day pacing.
3. **Misleading metadata.** Titles and thumbnails must match the video. The
   generator writes honest ones — don't "optimize" them into clickbait that
   the video can't deliver.
4. **Reused content (for monetization).** To join the Partner Program later,
   the channel needs original commentary and educational value, not compilations
   of others' work. Everything this tool makes is original — keep it that way
   and don't mix in downloaded clips.
5. **Browser-automation uploaders.** Tools that drive YouTube Studio with
   Selenium violate the Terms of Service and get channels terminated. Three
   minutes of manual uploading is the entire cost of staying compliant.

---

## Monetization reality check

- You need **1,000 subscribers + 4,000 watch hours** (or 10M Shorts views) for
  the Partner Program. AI channels get there the same way as any channel: good
  niche, good retention, consistency.
- AI-generated content **is** eligible for monetization if it's original and
  provides value — YouTube's policy targets low-effort spam, not the tools.
  Disclosure (step 4) is part of staying eligible.
- Don't buy subscribers or views. Ever. Detection is automatic and the penalty
  is channel termination with no appeal.
