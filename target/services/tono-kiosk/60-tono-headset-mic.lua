-- SPDX-License-Identifier: GPL-3.0
-- Avaota A1: the 3.5mm jack microphone is the codec's MIC3 and the only wired
-- input. The codec's ADC FIFO carries one channel per enabled input, so with
-- MIC3 alone (tono-headset-mic.service) the stereo fallback profile would
-- deliver misaligned samples; expose the capture node as mono instead.
-- WirePlumber 0.4 loads this after 50-alsa-config.lua defines alsa_monitor.
table.insert(alsa_monitor.rules, {
  matches = { { { "node.name", "equals", "alsa_input.platform-soc_3000000_codec_mach.stereo-fallback" } } },
  apply_properties = {
    ["audio.channels"] = 1,
    ["audio.position"] = "MONO",
    ["node.description"] = "Avaota A1 Headset Microphone",
  },
})
