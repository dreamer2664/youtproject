# Viral study: what actually goes viral in AI Shorts (and how we beat it)

Method: top TikTok results for "making money YouTube ai automation" + hand-picked
diamonds. Each entry: link, style, transcript gist, and the MECHANICS (not the
topic — mechanics transfer across niches).

## Video 1 — stickman listicle "3 skills" (17.7s)
Link: https://vm.tiktok.com/ZN8jP6D63/
- Style: paper-texture background, hand-drawn black stick figures in a NEW full
  scene per beat (waiter, reader, writer, money guy, classroom, multitasker).
- Text: GIANT kinetic keywords, two-tone (black + teal), top third. 2–3 words
  per shot: "Work For / You", "To Learn / Fast", "Won't Teach / This".
- Script: listicle ("learn these 3 skills before 18"), staccato, ends with
  "School won't teach this. I will. Follow for more."
- Mechanics: a shot change on EVERY sentence (~2–3s), keyword text readable on
  mute, us-vs-school framing, follow CTA.

## Video 2 — mascot satire "unethical money part 79" (73s)
Link: https://vm.tiktok.com/ZN8jPJs57/
- Style: plain white background, ONE recurring mascot (orange blob man), simple
  props pop in per beat (whiteboard, boxing gloves, crowd, Twitch logo).
- Text: big blue keyword captions, same top-third placement as video 1.
- Script: series format ("part 79"), rage-bait satire, "tag your friend" CTA in
  the first 5 seconds, "(Entertainment and Satire purposes only)" disclaimer.
- Mechanics: series numbering (follow for part 80), tag-a-friend distribution,
  rage = comments, mascot = brand recognition for near-zero art cost.

## The juice (patterns across entries)
1. THEY ARE SLIDESHOWS TOO. Both videos are static cartoon images + captions +
   voiceover. The "motion" feeling = shot changes every 2–4s synced to narration
   beats + kinetic text + subtle zoom. Our pipeline already does this shape —
   the gap is STYLE, not motion technology.
2. Kinetic keyword typography does half the retention work: giant 2–4 word
   two-tone text per shot, top third, readable on mute. Our karaoke captions
   are bottom-centered small — same job, weaker punch.
3. Beat-synced cuts: one visual per sentence, not per scene. We cut per scene
   (3 images/scene); they cut per sentence. We HAVE word timings — we can do this.
4. Mascot > variety (video 2): one consistent character beats 18 different
   images for brand + cost. A recurring character is a growth asset.
5. Series + tag mechanics (video 2): "part N" numbering and tag-a-friend CTAs
   are distribution features, not content. Ours to copy (tastefully).
6. Cartoon > photoreal for this genre: no uncanny valley, cheaper to make,
   reads instantly at thumbnail size. Photorealism is not an advantage here.

## What this means for youtproject
Have already: slideshow engine, beat timing data (word timings), captions,
music/CTA/scheduler/backlog. Missing: cartoon art direction, keyword overlays,
sentence-level cuts, mascot consistency. Nothing here requires paid AI video
(Runway/Veo/Pika) — the diamonds prove that would be overkill.

## Upgrade plan (proposal — finalize after all 10 entries)
- P0 ✅ SHIPPED: `video.style` config flag + `--style` CLI override
  (`photoreal` default | `cartoon` | `stickman`). Image prompts, Gemini script
  brief and per-style shot framings all flip with it (images.py STYLES).
  Default stays photoreal, so existing channels are untouched.
- P0 cartoon style switch: image prompts + script brief flip from "photorealistic,
  cinematic" to flat-vector cartoon / whiteboard-stickman look. One config flag,
  instant genre flip. (Cheapest win on this page.)
- P1 kinetic keywords: Gemini picks 2–4 keyword words per scene; burned as giant
  two-tone top-third text (drawtext), replacing/augmenting bottom karaoke.
- P2 sentence-level cuts: slice scenes on sentence boundaries using TTS word
  timings; one cartoon image per sentence instead of per scene.
- P3 mascot mode (optional): recurring character brief injected into every image
  prompt for brand consistency across a channel.
- P4 motion graphics: pop-in text scale, punch-in transitions, film grain.
- P5 (later, real animation): procedural stickman animator — PIL-drawn mascot
  with tweened poses + stroke-reveal drawing effect. True motion, still €0.

## Pending entries (slots for the top-10 links)
3. (link) —
4. (link) —
5. (link) —
6. (link) —
7. (link) —
8. (link) —
9. (link) —
10. (link) —
