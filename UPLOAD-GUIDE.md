# UPLOAD-GUIDE — manual YouTube Studio upload

The generator creates local files only. Each `upload/<id>/` kit includes a
video, copy-paste metadata, an optional subtitle file, and a per-video
`CHECKLIST.md`. Nothing here signs in to YouTube or uploads/publishes for you.

This guide is intentionally YouTube-only. Review YouTube Studio's current
labels and help pages when the interface or rules change.

## Important: the API audit is not per channel

YouTube's restriction concerns uploads made through the YouTube Data API
`videos.insert` endpoint by an unverified API project created after 28 July
2020. The restriction is lifted through an audit of the **API project**—the
official documentation says each API project must undergo the audit. It is
not an audit you submit separately for every channel.

This project does not call `videos.insert`. YouTube's Help says that, for a
video locked private because it was uploaded through an unverified API
service, the creator can re-upload through the YouTube app/site; that is the
legitimate route this manual workflow uses. This is not a way to disguise an
API upload or evade policy—it simply uses YouTube's own Studio/site upload.
Separate channel feature eligibility or identity/phone verification may still
apply to particular Studio features, such as custom thumbnails.

Official references:
- [Videos: insert — project audit restriction](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Videos locked as private — YouTube Help](https://support.google.com/youtube/answer/7300965?hl=en)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)

## 1. Before uploading

1. Make sure you are signed in to the intended channel in YouTube Studio.
2. Watch the complete exported video with sound. Check the opening, ending,
   image changes, burned-in text, audio levels, and subtitle timing.
3. Review every factual claim against reliable, independent material. The
   optional in-pipeline model review is a plausibility pass only: it has no
   source documents, does not verify facts, and cannot replace your review.
4. Check rights for every video, image, audio track, and source clip. Credit is
   not permission; a clip kit's source credit does not guarantee copyright or
   reused-content compliance. Fair use is fact-specific.
5. Confirm the title, description, and thumbnail accurately represent the
   finished video. Remove unsupported claims and misleading tags.

## 2. Upload the file

1. Open [YouTube Studio](https://studio.youtube.com/) and choose **Create →
   Upload videos** (or use YouTube's current upload page).
2. Select `video.mp4` from the kit and wait for upload/processing to finish.
3. Keep the upload private or unlisted while you review the details if you
   prefer; choose visibility yourself after the checks below.

## 3. Details

- **Title** — copy `title.txt`; the kit enforces YouTube's 100-character
  maximum. Read it once and remove any claim the video does not support.
- **Description** — copy `description.txt`. Its optional AI-tools note is a
  transparency note only. It is not a substitute for Studio's AI-use answer.
- **Thumbnail** — use `thumbnail.jpg` if present and available for that video
  type/account. If it is absent or Studio does not offer a custom thumbnail,
  choose an appropriate frame using Studio's available controls.
- **Tags** — copy `tags.txt` if you choose to use tags. They are optional and
  do not guarantee discovery or reach. Keep every tag relevant.
- **Language/category** — use the values in `CHECKLIST.md` when appropriate;
  correct them if they do not describe the finished video.

## 4. YouTube Studio's AI-use setting — decide per video

Answer from the finished video's content, not just from the fact that an AI
tool was used. YouTube says disclosure is required when AI meaningfully
alters or generates realistic content that could mislead viewers. Its examples
include:

- making a real person appear to say or do something they did not;
- altering footage of a real event or place;
- generating a realistic scene that did not happen; or
- AI-generated music that is the main focus of the video.

Clearly fantastical/stylized content, minor aesthetic edits, and some
production assistance may not require disclosure. The examples are not
exhaustive. Inspect the actual visuals and audio; photorealistic invented
scenes are a common reason this project's videos may need a **Yes** answer.
In Studio, under **Attributes → AI use** (or the equivalent label shown in
your version), choose **Yes** if the criteria apply and **No** if they do not.
Do not select Yes automatically just because the script, narration, or images
used AI tools—and do not select No automatically either.

See [YouTube's official GenAI disclosure guidance](https://support.google.com/youtube/answer/14328491?hl=en).

## 5. Captions

If the kit contains `captions.srt`, upload it in Studio's subtitles/captions
controls and review the result. Burned-in words are part of the video picture;
an SRT is a separate, viewer-controllable caption track. If no SRT is present,
do not claim that one was uploaded—use Studio's available caption tools and
check any automatic transcription for errors.

## 6. Audience, visibility, and publish

- Answer the **made for kids** question based on the video's intended audience
  and YouTube's current guidance; documentary style alone does not determine
  the answer.
- Select Public, Unlisted, Private, or Schedule intentionally. There is no
  guaranteed upload time or posting cadence that makes a video perform well.
- Review all fields once more, then make the final publish/schedule decision
  yourself.

## 7. Record the URL locally

After a successful manual upload, record its URL so the local queue remains
accurate:

```bash
python main.py published <id> https://youtu.be/PASTE-ID-HERE
```

## If you are using the clips/parts/longform lanes

Those kits are also YouTube-only. They preserve source URLs/time windows where
available, but you are responsible for confirming rights and permissions
before uploading. Attribution is useful context, not a guarantee that a clip
is permitted or sufficiently original. Read YouTube's current copyright,
Community Guidelines, and monetization/reused-content guidance before using
third-party footage.

## Monetization and policy

Eligibility and review requirements can change. Do not rely on fixed numbers
or a tool's claims about monetization. Check [YouTube Partner Program
policies](https://support.google.com/youtube/answer/72851?hl=en),
[Community Guidelines](https://www.youtube.com/howyoutubeworks/policies/community-guidelines/),
and [YouTube monetization policies](https://support.google.com/youtube/answer/1311392?hl=en)
for current rules. AI disclosure does not itself decide monetization eligibility;
content still has to comply with the policies that apply to all uploads.
