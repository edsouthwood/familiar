# The fifteen forms

The shapes the *find familiar* spell allows, one file each, in the order the
spell lists them — "bat, cat, crab, frog (toad), hawk, lizard, octopus, owl,
poisonous snake, fish (quipper), rat, raven, sea horse, spider, or weasel".
Where the spell gives two words, the filename is the one it leads with and the
other is in that file's `<title>`: `frog.svg` is titled *Frog (toad)*,
`fish.svg` *Fish (quipper)*.

| | | |
|---|---|---|
| `bat.svg` | `cat.svg` | `crab.svg` |
| `frog.svg` | `hawk.svg` | `lizard.svg` |
| `octopus.svg` | `owl.svg` | `poisonous-snake.svg` |
| `fish.svg` | `rat.svg` | `raven.svg` |
| `sea-horse.svg` | `spider.svg` | `weasel.svg` |

Each is a 64×64 line drawing: one stroke weight, no fill, `currentColor`, and
a `<title>` for screen readers. The symmetric ones (bat, crab, frog, hawk,
octopus, owl, spider) are a right half reflected through a `<use>`, so the two
halves cannot drift apart when one is edited.

They are kept here as a set so that any page that wants a creature can take
one without redrawing it.

## Putting one on a page

**Inline**, if you want it to take the colour of the text around it — this is
the one that always works, and it is what `currentColor` is for:

```html
<svg class="form" viewBox="0 0 64 64" role="img" aria-label="Owl">…</svg>
```

Copy the marks out of the file. Note that `<use href="/forms/owl.svg#…">`
against an *external* file still fails in Safari, so an SVG sprite is not an
option for a page a stranger loads cold.

**As a CSS mask**, if you want the colour to come from a token — the drawing
becomes a hole in a flat fill, so it can never drift from the palette:

```css
.owl {
  width: 40px; height: 40px;
  background: var(--text-dim);
  -webkit-mask: url(/forms/owl.svg) no-repeat center / contain;
  mask: url(/forms/owl.svg) no-repeat center / contain;
}
```

**As `<img src="/forms/owl.svg">`** only where black is what you want:
`currentColor` in an image has no text to inherit from and resolves to black.
On these pages that is invisible, which is why the mask above is the usual
answer.
