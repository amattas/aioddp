# Sender

`DDPSender` owns the socket to one controller. Every frame runs through the
color pipeline, so the strip's brightness cap always applies.

## Keepalive and release

Some controllers blank, or fall back to a built-in effect, when DDP data stops.
`DDPSender(strip, keepalive_interval=5.0)` resends what it holds after that many
idle seconds.

The sender holds the byte ranges it has sent to: every `send_frame`, `fill`, and
`off` holds its segment (black from `off` is held too). A keepalive resends only
the held ranges, at their offsets, and latches once per burst. Nothing held
means no keepalive.

`release(segment)` drops one segment's pixels from the held set, so a controller
can run its own effect there while DDP keeps the rest alive. `release()` drops
everything. Neither sends anything; the next frame to those pixels holds them
again. `tx.held` reads the held byte ranges as a sorted tuple of
`(offset, length)`; `()` means keepalive has nothing to resend.

```python
async with DDPSender(strip, keepalive_interval=5.0) as tx:
    await tx.fill(roof, (255, 140, 40))
    await tx.off(peak)
    tx.release(peak)  # keepalive now resends only the roofline
```
