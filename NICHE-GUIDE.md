# Niche guide: where the growth actually is (September 2026)

Short version: **psychology facts**. It fits this pipeline 1:1 (facts format,
AI-imageable, endless topics, comment-baitable), and "easiest growth" sources
keep naming psychology alongside stoicism and mythology for beginners.
Queue the starter pack today:

```powershell
python main.py batch --topics topics/psychology-facts.txt --seconds 35
```

## How Shorts get pushed in 2026 (what changed)

- **Completed views + rewatches are everything.** Since March 2025 every replay
  of a Short counts as another view, and replays are weighted heavily as an
  engagement signal. A loopable 30s Short watched twice beats a 60s Short
  watched once. ([virvid](https://virvid.ai/blog/looping-structure-shorts-retention-2026),
  [socialync](https://www.socialync.io/blog/youtube-shorts-algorithm-2026))
- **30–45 seconds is the sweet spot.** Under 30s you need ~65% average view
  duration to pass the gate; over 30s the bar is ~50%. Easier target, more
  ad inventory. ([socialync](https://www.socialync.io/blog/youtube-shorts-algorithm-2026))
- **Cut every 3–4 seconds.** Same frame for 4s+ and viewers swipe. This
  pipeline's `images_per_scene: 3` plus the whooshes exist precisely for this.
  ([socialync](https://www.socialync.io/blog/youtube-shorts-algorithm-2026))
- **Burned captions are mandatory**, not a bonus — most Shorts start muted.
  Already on by default here. ([teleprompter](https://www.teleprompter.com/blog/how-to-go-viral-on-youtube-shorts))

## The pick: psychology facts (why-we-do-it channel)

| Question | Answer |
|---|---|
| Why this niche | Infinite topics, zero expertise needed, every script ends in a "wait, really?" moment that earns comments |
| RPM | Mid-tier (~$3–6 long-form; Shorts pay a fraction — see below) |
| Competition | Crowded at the top, thin in specific angles: dark psychology, brain glitches, social experiments |
| Format fit | Perfect: hook → 3 facts → twist → CTA. Exactly what the generator writes |

**Settings recipe** (`config.yaml`):

```yaml
channel:
  topic: "short psychology facts about why humans do weird things"
  tone: "fast, punchy, high-energy"
  target_seconds: 35
  speech_rate: "+20%"
video:
  images_per_scene: 3
```

## The runners-up (if psychology bores you)

| Niche | Retention | RPM | Verdict |
|---|---|---|---|
| Creepy mysteries & unsolved | Highest — suspense glues people | Mid ($2–4) | Best videos, slowest growth. Pack: `topics/creepy-mysteries.txt`. Use tone `dark and dramatic`. |
| Animal facts | Very high, family-safe, evergreen | Low-mid ($2–5) | Your jellyfish video is the prototype. Pack: `topics/animal-facts.txt`. |
| Stoicism / motivation | Medium, saturated | Mid ($4–7) | Easiest to write, hardest to stand out. Only with a sharp angle. |
| AI & tech explainers | Medium | High ($4–12) | Best money, but content ages in weeks and needs accuracy. Graduate here later. |

Sources for RPM/CPM bands:
[clipspeed](https://www.clipspeed.ai/blog/faceless-youtube-niches-2026.html),
[flowshorts](https://flowshorts.app/faceless-youtube-channel-ideas),
[faceless.my](https://faceless.my/niches/top-faceless-youtube-niches/),
[reelforge](https://reelforgeai.io/blog/50-profitable-faceless-youtube-niches-2026.html).

Avoid for now: finance (highest CPM but demands real expertise and trust),
true crime (monetization-sensitive), cooking/gaming (brutal competition,
mid-low CPM).

## The only 3 numbers to watch (YouTube Studio → Analytics → Shorts)

1. **Average percentage viewed** — the gate. Below 50% on 30s+ videos: your
   hook or pacing is the problem, not the topic. Fix: shorter sentences,
   `images_per_scene: 4`, cut dead air.
2. **Rewatches / "watched vs swiped away"** — the multiplier. If % viewed is
   high but views stall, the ending isn't loopable. Experiment: `cta.enabled:
   false` on a few videos so the ending flows back into the hook.
3. **Subscribers gained per 1,000 views** — the compounding. Below ~1 sub/1k:
   the CTA isn't landing. Edit `cta.lines` to be more niche-specific
   ("Comment your attachment style 👇" beats "comment below").

Ignore everything else for the first 30 days.

## The 30-day plan (1–2 videos/day, ~15 min of your time)

- **Days 1–7:** render the psychology pack (`batch` command above), upload 2/day,
  keep every setting identical. You're calibrating, not optimizing.
- **Days 8–14:** read the 3 numbers. Kill the worst-performing angle, double
  the best. Tweak ONE thing (hook style, voice, or CTA lines).
- **Days 15–30:** settle into 1–2/day from your phone (`/queue` topics to the
  bot whenever inspiration strikes). Consistency beats perfection — the
  algorithm responds to sustained posting, which is exactly what automation
  makes possible.

## Honest money expectations

Shorts RPM is roughly **$0.04–0.15** — a million Shorts views is lunch money,
not rent. Shorts are the audience engine; the money comes later from long-form
videos fed by that audience, affiliates, or sponsors. Anyone promising otherwise
is selling a course. Grow first (this pipeline), monetize second.
