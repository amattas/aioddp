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

## Frame repeats

Some controllers ignore a lone frame that arrives after a gap in the stream and
only show data once frames keep coming. `DDPSender(strip, repeat_frames=3)`
sends every frame as a burst: the frame itself, then `repeat_frames - 1`
resends of the held ranges, `repeat_interval` seconds apart (default 0.1,
at least 0.01). The default `repeat_frames=1` sends each frame once.

The resends come from a background task, so `send_frame`, `fill`, and `off`
return as soon as the frame itself is sent. Each resend reads the held ranges
at that moment, exactly like a keepalive burst (latching once), so a
`release()` stops repeats of the released pixels. A newer frame restarts the
countdown: an effect streaming faster than `repeat_interval` adds no packets,
and its last frame is repeated once it stops. `close()` cancels pending
repeats. Repeats count in `stats.repeats_sent` and as sends for keepalive.

`release()` stops the released pixels at once, a scheduled repeat included,
which suits handing them to a controller's own effect. To release pixels right
after their last frame (black after turning a light off, say) without cutting
that frame's burst short, use `release(segment, drain=True)`: the pending
burst still resends them, then they stop. They leave `held` and keepalive at
once; a frame to them in the meantime holds them again. With no burst pending
(or `repeat_frames=1`), `drain=True` releases at once.

```python
async with DDPSender(strip, repeat_frames=3, repeat_interval=0.1) as tx:
    await tx.fill(roof, (255, 140, 40))  # on the wire at 0, 0.1 and 0.2 s
```
