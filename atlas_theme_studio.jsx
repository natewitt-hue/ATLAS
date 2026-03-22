import { useState, useRef, useEffect, useCallback } from "react";

// ═══════════════════════════════════════════════════════════════
// DISPLAY FONTS — categorized. "custom" entries loaded at runtime.
// To add more: just append to this array or use the Custom Font input.
// Browse https://fonts.google.com for names.
// ═══════════════════════════════════════════════════════════════
const GOOGLE_FONTS = [
  // ── Tech / Geometric Sans ──
  { label: "Outfit", value: "Outfit", weights: "400;600;700;800", category: "tech" },
  { label: "Rajdhani", value: "Rajdhani", weights: "500;600;700", category: "tech" },
  { label: "Teko", value: "Teko", weights: "500;600;700", category: "tech" },
  { label: "Orbitron", value: "Orbitron", weights: "500;600;700;800;900", category: "tech" },
  { label: "Exo 2", value: "Exo+2", weights: "500;600;700;800;900", category: "tech" },
  { label: "Chakra Petch", value: "Chakra+Petch", weights: "500;600;700", category: "tech" },
  { label: "Audiowide", value: "Audiowide", weights: "400", category: "tech" },
  { label: "Aldrich", value: "Aldrich", weights: "400", category: "tech" },
  { label: "Michroma", value: "Michroma", weights: "400", category: "tech" },
  { label: "Quantico", value: "Quantico", weights: "400;700", category: "tech" },
  { label: "Jura", value: "Jura", weights: "400;500;600;700", category: "tech" },
  { label: "Electrolize", value: "Electrolize", weights: "400", category: "tech" },
  { label: "Oxanium", value: "Oxanium", weights: "400;500;600;700;800", category: "tech" },
  { label: "Saira", value: "Saira", weights: "400;500;600;700;800;900", category: "tech" },
  { label: "Play", value: "Play", weights: "400;700", category: "tech" },
  { label: "Kanit", value: "Kanit", weights: "400;500;600;700;800;900", category: "tech" },
  { label: "Russo One", value: "Russo+One", weights: "400", category: "tech" },
  { label: "Bai Jamjuree", value: "Bai+Jamjuree", weights: "400;500;600;700", category: "tech" },
  { label: "Stalinist One", value: "Stalinist+One", weights: "400", category: "tech" },
  { label: "Bruno Ace", value: "Bruno+Ace", weights: "400", category: "tech" },
  // ── Clean Sans ──
  { label: "DM Sans", value: "DM+Sans", weights: "400;500;600;700", category: "sans" },
  { label: "Plus Jakarta Sans", value: "Plus+Jakarta+Sans", weights: "400;500;600;700;800", category: "sans" },
  { label: "Sora", value: "Sora", weights: "400;500;600;700;800", category: "sans" },
  { label: "General Sans (Manrope)", value: "Manrope", weights: "400;500;600;700;800", category: "sans" },
  { label: "Urbanist", value: "Urbanist", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Figtree", value: "Figtree", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Lexend", value: "Lexend", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Albert Sans", value: "Albert+Sans", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Space Grotesk", value: "Space+Grotesk", weights: "400;500;600;700", category: "sans" },
  { label: "Red Hat Display", value: "Red+Hat+Display", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Archivo", value: "Archivo", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Barlow", value: "Barlow", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Barlow Condensed", value: "Barlow+Condensed", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Montserrat", value: "Montserrat", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Poppins", value: "Poppins", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Nunito", value: "Nunito", weights: "400;500;600;700;800;900", category: "sans" },
  { label: "Josefin Sans", value: "Josefin+Sans", weights: "400;500;600;700", category: "sans" },
  // ── Serif / Elegant ──
  { label: "Cinzel", value: "Cinzel", weights: "600;700;800;900", category: "serif" },
  { label: "Cinzel Decorative", value: "Cinzel+Decorative", weights: "700;900", category: "serif" },
  { label: "Playfair Display", value: "Playfair+Display", weights: "700;800;900", category: "serif" },
  { label: "Cormorant Garamond", value: "Cormorant+Garamond", weights: "500;600;700", category: "serif" },
  { label: "Cormorant SC", value: "Cormorant+SC", weights: "400;500;600;700", category: "serif" },
  { label: "Lora", value: "Lora", weights: "500;600;700", category: "serif" },
  { label: "DM Serif Display", value: "DM+Serif+Display", weights: "400", category: "serif" },
  { label: "Libre Baskerville", value: "Libre+Baskerville", weights: "400;700", category: "serif" },
  { label: "Crimson Pro", value: "Crimson+Pro", weights: "400;500;600;700;800;900", category: "serif" },
  { label: "Spectral", value: "Spectral", weights: "400;500;600;700;800", category: "serif" },
  { label: "Fraunces", value: "Fraunces", weights: "400;500;600;700;800;900", category: "serif" },
  { label: "Bodoni Moda", value: "Bodoni+Moda", weights: "400;500;600;700;800;900", category: "serif" },
  { label: "Merriweather", value: "Merriweather", weights: "400;700;900", category: "serif" },
  { label: "Bitter", value: "Bitter", weights: "400;500;600;700;800;900", category: "serif" },
  { label: "EB Garamond", value: "EB+Garamond", weights: "400;500;600;700;800", category: "serif" },
  // ── Display / Decorative ──
  { label: "Special Elite", value: "Special+Elite", weights: "400", category: "display" },
  { label: "Bebas Neue", value: "Bebas+Neue", weights: "400", category: "display" },
  { label: "Permanent Marker", value: "Permanent+Marker", weights: "400", category: "display" },
  { label: "Righteous", value: "Righteous", weights: "400", category: "display" },
  { label: "Rubik Mono One", value: "Rubik+Mono+One", weights: "400", category: "display" },
  { label: "Black Ops One", value: "Black+Ops+One", weights: "400", category: "display" },
  { label: "Bungee", value: "Bungee", weights: "400", category: "display" },
  { label: "Bungee Shade", value: "Bungee+Shade", weights: "400", category: "display" },
  { label: "Press Start 2P", value: "Press+Start+2P", weights: "400", category: "display" },
  { label: "Silkscreen", value: "Silkscreen", weights: "400;700", category: "display" },
  { label: "VT323", value: "VT323", weights: "400", category: "display" },
  { label: "Major Mono Display", value: "Major+Mono+Display", weights: "400", category: "display" },
  { label: "Rubik Glitch", value: "Rubik+Glitch", weights: "400", category: "display" },
  { label: "Monoton", value: "Monoton", weights: "400", category: "display" },
  { label: "Fascinate", value: "Fascinate", weights: "400", category: "display" },
  { label: "Pirata One", value: "Pirata+One", weights: "400", category: "display" },
  { label: "Creepster", value: "Creepster", weights: "400", category: "display" },
  { label: "Metal Mania", value: "Metal+Mania", weights: "400", category: "display" },
  { label: "Nosifer", value: "Nosifer", weights: "400", category: "display" },
  { label: "UnifrakturMaguntia", value: "UnifrakturMaguntia", weights: "400", category: "display" },
  { label: "MedievalSharp", value: "MedievalSharp", weights: "400", category: "display" },
  { label: "Abril Fatface", value: "Abril+Fatface", weights: "400", category: "display" },
  { label: "Alfa Slab One", value: "Alfa+Slab+One", weights: "400", category: "display" },
  { label: "Changa One", value: "Changa+One", weights: "400", category: "display" },
  { label: "Codystar", value: "Codystar", weights: "400", category: "display" },
  { label: "Faster One", value: "Faster+One", weights: "400", category: "display" },
  // ── Condensed / Impact ──
  { label: "Oswald", value: "Oswald", weights: "400;500;600;700", category: "condensed" },
  { label: "Anton", value: "Anton", weights: "400", category: "condensed" },
  { label: "Pathway Extreme", value: "Pathway+Extreme", weights: "400;500;600;700;800;900", category: "condensed" },
  { label: "Big Shoulders Display", value: "Big+Shoulders+Display", weights: "400;500;600;700;800;900", category: "condensed" },
  { label: "Saira Condensed", value: "Saira+Condensed", weights: "400;500;600;700;800;900", category: "condensed" },
  { label: "Fjalla One", value: "Fjalla+One", weights: "400", category: "condensed" },
  { label: "Alumni Sans", value: "Alumni+Sans", weights: "400;500;600;700;800;900", category: "condensed" },
  { label: "Encode Sans Condensed", value: "Encode+Sans+Condensed", weights: "400;500;600;700;800;900", category: "condensed" },
];

// ═══════════════════════════════════════════════════════════════
// MONO FONTS
// ═══════════════════════════════════════════════════════════════
const MONO_FONTS = [
  { label: "JetBrains Mono", value: "JetBrains+Mono", weights: "400;600;700;800" },
  { label: "IBM Plex Mono", value: "IBM+Plex+Mono", weights: "500;600;700" },
  { label: "Fira Mono", value: "Fira+Mono", weights: "400;500;700" },
  { label: "Fira Code", value: "Fira+Code", weights: "400;600;700" },
  { label: "Space Mono", value: "Space+Mono", weights: "400;700" },
  { label: "Source Code Pro", value: "Source+Code+Pro", weights: "400;600;700" },
  { label: "Share Tech Mono", value: "Share+Tech+Mono", weights: "400" },
  { label: "DM Mono", value: "DM+Mono", weights: "400;500" },
  { label: "Courier Prime", value: "Courier+Prime", weights: "400;700" },
  { label: "Roboto Mono", value: "Roboto+Mono", weights: "400;500;700" },
  { label: "Inconsolata", value: "Inconsolata", weights: "400;600;700;800" },
  { label: "Ubuntu Mono", value: "Ubuntu+Mono", weights: "400;700" },
  { label: "Overpass Mono", value: "Overpass+Mono", weights: "400;500;600;700" },
  { label: "Red Hat Mono", value: "Red+Hat+Mono", weights: "400;500;600;700" },
  { label: "Noto Sans Mono", value: "Noto+Sans+Mono", weights: "400;500;600;700;800;900" },
  { label: "Azeret Mono", value: "Azeret+Mono", weights: "400;500;600;700;800;900" },
  { label: "Martian Mono", value: "Martian+Mono", weights: "400;500;600;700;800" },
  { label: "Sometype Mono", value: "Sometype+Mono", weights: "400;500;600;700" },
  { label: "Cutive Mono", value: "Cutive+Mono", weights: "400" },
  { label: "Anonymous Pro", value: "Anonymous+Pro", weights: "400;700" },
  { label: "VT323", value: "VT323", weights: "400" },
  { label: "Press Start 2P", value: "Press+Start+2P", weights: "400" },
  { label: "Silkscreen", value: "Silkscreen", weights: "400;700" },
  { label: "Major Mono Display", value: "Major+Mono+Display", weights: "400" },
];

// Font category labels and colors for the filter UI
const FONT_CATEGORIES = [
  { id: "all", label: "All", color: "#888" },
  { id: "tech", label: "Tech", color: "#5AB8E0" },
  { id: "sans", label: "Sans", color: "#8ED47A" },
  { id: "serif", label: "Serif", color: "#C9A8FF" },
  { id: "display", label: "Display", color: "#FFA94D" },
  { id: "condensed", label: "Condensed", color: "#F0A8BE" },
  { id: "custom", label: "Custom", color: "#FFE8A0" },
];

const PANEL_STYLES = [
  { id: "glass", label: "Glass" },
  { id: "inset", label: "Inset Shadow" },
  { id: "bordered", label: "Bordered" },
  { id: "leftStripe", label: "Left Stripe" },
  { id: "minimal", label: "Minimal" },
  { id: "bottomAccent", label: "Bottom Accent" },
];

const DIVIDER_STYLES = [
  { id: "gradient", label: "Gradient Line" },
  { id: "ornament", label: "◆ Ornament" },
  { id: "crest", label: "♛ Crest" },
  { id: "dots", label: "· · · Dots" },
  { id: "doubleLine", label: "Double Line" },
  { id: "leftHeavy", label: "Left Heavy" },
  { id: "radial", label: "Radial Fade" },
  { id: "dotPattern", label: "Dot Pattern" },
];

const CORNER_STYLES = [
  { id: "none", label: "None" },
  { id: "hud", label: "HUD Brackets" },
  { id: "crosshair", label: "Crosshair" },
  { id: "deco", label: "Art Deco ╔" },
];

const HERO_STYLES = [
  { id: "vertGradient", label: "Vertical Gradient" },
  { id: "diagGradient", label: "Diagonal Gradient" },
  { id: "glow", label: "Pure Glow" },
  { id: "stamp", label: "Stamp Shadow" },
  { id: "engraved", label: "Engraved Metal" },
  { id: "rainbow", label: "Rainbow Gradient" },
  { id: "italic", label: "Italic + Drip" },
];

const OVERLAY_OPTIONS = [
  { id: "scanlines", label: "Scanlines" },
  { id: "hexgrid", label: "Hex Grid" },
  { id: "sonar", label: "Sonar Rings" },
  { id: "starfield", label: "Star Field" },
  { id: "crtLines", label: "CRT Lines" },
  { id: "heavyGrain", label: "Heavy Grain" },
];

const VIGNETTE_OPTIONS = [
  { id: "none", label: "None" },
  { id: "warm", label: "Warm" },
  { id: "cool", label: "Cool" },
  { id: "heavy", label: "Heavy" },
];

const BADGE_SHAPES = [
  { id: "rounded", label: "Rounded (8px)" },
  { id: "pill", label: "Pill (20px)" },
  { id: "square", label: "Square (2px)" },
  { id: "dashed", label: "Dashed" },
];

const ICON_SHAPES = [
  { id: "rounded", label: "Rounded Square" },
  { id: "circle", label: "Circle" },
  { id: "square", label: "Square" },
];

const STATUS_HEIGHTS = [
  { id: "2", label: "Thin (2px)" },
  { id: "3", label: "Normal (3px)" },
  { id: "4", label: "Medium (4px)" },
  { id: "6", label: "Thick (6px)" },
];

const DEFAULT_THEME = {
  name: "New Theme",
  emoji: "✨",
  colors: {
    bg: "#0D0D0F", gold: "#E2C05C", bright: "#F5DFA0", dim: "#9E8B4E", light: "#F5DFA0",
    win: "#34D399", winDk: "#059669", loss: "#FB7185", lossDk: "#E11D48",
    txt: "#F0EAD6", sub: "#6B6458", muted: "#9E8B4E", dimTxt: "#6B6458",
    panelBg: "rgba(255,255,255,0.04)", panelBorder: "rgba(255,255,255,0.08)",
  },
  displayFont: "Outfit",
  monoFont: "JetBrains+Mono",
  borderRadius: 8,
  panelStyle: "bordered",
  dividerStyle: "gradient",
  cornerStyle: "hud",
  heroStyle: "diagGradient",
  overlays: [],
  vignette: "warm",
  badgeShape: "rounded",
  iconShape: "rounded",
  statusHeight: "4",
  heroSize: 48,
  labelStyle: "uppercase",
  statusBarInset: false,
};

const PRESETS = {
  frozen_throne: {
    name: "Frozen Throne", emoji: "🧊",
    colors: { bg: "#080C14", gold: "#7EB8D4", bright: "#B8E2F8", dim: "#3E6A82", light: "#D4F0FF", win: "#4EEAAA", winDk: "#1A7A5A", loss: "#E85A6F", lossDk: "#8A2236", txt: "#D8E6EE", sub: "#6A8A9E", muted: "#3E5A6E", dimTxt: "#243444", panelBg: "rgba(126,184,212,0.05)", panelBorder: "rgba(126,184,212,0.12)" },
    displayFont: "Rajdhani", monoFont: "IBM+Plex+Mono", borderRadius: 3, panelStyle: "inset", dividerStyle: "gradient", cornerStyle: "hud", heroStyle: "vertGradient", overlays: ["hexgrid"], vignette: "none", badgeShape: "square", iconShape: "square", statusHeight: "3", heroSize: 52, labelStyle: "uppercase", statusBarInset: false,
  },
  blood_ledger: {
    name: "Blood Ledger", emoji: "🩸",
    colors: { bg: "#0C0607", gold: "#C43A3A", bright: "#F05555", dim: "#6E2222", light: "#FF8A8A", win: "#E6B84D", winDk: "#8A6E1E", loss: "#5B8EDB", lossDk: "#2A4470", txt: "#EEDADA", sub: "#9A7878", muted: "#6A4444", dimTxt: "#3E2424", panelBg: "rgba(196,58,58,0.04)", panelBorder: "rgba(196,58,58,0.10)" },
    displayFont: "Playfair+Display", monoFont: "Fira+Mono", borderRadius: 6, panelStyle: "inset", dividerStyle: "ornament", cornerStyle: "none", heroStyle: "italic", overlays: ["heavyGrain"], vignette: "heavy", badgeShape: "pill", iconShape: "circle", statusHeight: "4", heroSize: 50, labelStyle: "uppercase", statusBarInset: false,
  },
  sovereign: {
    name: "Sovereign", emoji: "👑",
    colors: { bg: "#0A0914", gold: "#9B7BD4", bright: "#C9A8FF", dim: "#5A4480", light: "#E0D0FF", win: "#5ADB8A", winDk: "#267A48", loss: "#E86A4A", lossDk: "#8A3420", txt: "#E2DCF0", sub: "#8878A0", muted: "#5A4E70", dimTxt: "#342A48", panelBg: "rgba(155,123,212,0.04)", panelBorder: "rgba(155,123,212,0.10)" },
    displayFont: "Cinzel", monoFont: "Source+Code+Pro", borderRadius: 2, panelStyle: "bottomAccent", dividerStyle: "crest", cornerStyle: "none", heroStyle: "engraved", overlays: [], vignette: "none", badgeShape: "square", iconShape: "square", statusHeight: "3", heroSize: 46, labelStyle: "uppercase", statusBarInset: true,
  },
  ember_vault: {
    name: "Ember Vault", emoji: "🌋",
    colors: { bg: "#0C0806", gold: "#E8822A", bright: "#FFA94D", dim: "#7A4418", light: "#FFCC80", win: "#7AE85A", winDk: "#3A8A22", loss: "#D44A6A", lossDk: "#7A2040", txt: "#F0E0D2", sub: "#9A7A60", muted: "#6A4A34", dimTxt: "#3E2A1A", panelBg: "rgba(232,130,42,0.04)", panelBorder: "rgba(232,130,42,0.10)" },
    displayFont: "Teko", monoFont: "Share+Tech+Mono", borderRadius: 4, panelStyle: "leftStripe", dividerStyle: "leftHeavy", cornerStyle: "none", heroStyle: "vertGradient", overlays: [], vignette: "warm", badgeShape: "square", iconShape: "rounded", statusHeight: "6", heroSize: 62, labelStyle: "uppercase", statusBarInset: false,
  },
  abyss: {
    name: "Abyss", emoji: "🐙",
    colors: { bg: "#050910", gold: "#2AD4D4", bright: "#5AFAFF", dim: "#146A6E", light: "#A0FFFF", win: "#A4E84D", winDk: "#527A22", loss: "#E84D8A", lossDk: "#7A2248", txt: "#D0E8EE", sub: "#6A8A94", muted: "#3E5A64", dimTxt: "#1E3840", panelBg: "rgba(42,212,212,0.04)", panelBorder: "rgba(42,212,212,0.08)" },
    displayFont: "Orbitron", monoFont: "Space+Mono", borderRadius: 12, panelStyle: "glass", dividerStyle: "radial", cornerStyle: "none", heroStyle: "glow", overlays: ["sonar"], vignette: "cool", badgeShape: "pill", iconShape: "circle", statusHeight: "4", heroSize: 44, labelStyle: "uppercase", statusBarInset: false,
  },
  sakura_dusk: {
    name: "Sakura Dusk", emoji: "🌸",
    colors: { bg: "#0D0A0C", gold: "#D4829A", bright: "#F0A8BE", dim: "#7A4458", light: "#FFD0DE", win: "#7AD4A0", winDk: "#3A7A54", loss: "#D4A44A", lossDk: "#7A5E22", txt: "#F0E4E8", sub: "#A0868E", muted: "#6A5058", dimTxt: "#3E2E34", panelBg: "rgba(212,130,154,0.03)", panelBorder: "rgba(212,130,154,0.07)" },
    displayFont: "Cormorant+Garamond", monoFont: "DM+Mono", borderRadius: 16, panelStyle: "minimal", dividerStyle: "dotPattern", cornerStyle: "none", heroStyle: "glow", overlays: [], vignette: "none", badgeShape: "pill", iconShape: "circle", statusHeight: "2", heroSize: 50, labelStyle: "uppercase", statusBarInset: true,
  },
  smoke_amber: {
    name: "Smoke & Amber", emoji: "🕵️",
    colors: { bg: "#0B0A08", gold: "#CCA44A", bright: "#F0CC6A", dim: "#6E5828", light: "#FFE8A0", win: "#5ABE8A", winDk: "#2A6E4A", loss: "#BE5A5A", lossDk: "#6E2A2A", txt: "#E8E0D0", sub: "#8A8070", muted: "#5A5240", dimTxt: "#342E22", panelBg: "rgba(204,164,74,0.04)", panelBorder: "rgba(204,164,74,0.07)" },
    displayFont: "Special+Elite", monoFont: "Courier+Prime", borderRadius: 4, panelStyle: "bordered", dividerStyle: "dots", cornerStyle: "none", heroStyle: "stamp", overlays: ["scanlines", "heavyGrain"], vignette: "heavy", badgeShape: "dashed", iconShape: "rounded", statusHeight: "4", heroSize: 48, labelStyle: "uppercase", statusBarInset: false,
  },
  nebula: {
    name: "Nebula", emoji: "🔮",
    colors: { bg: "#08061A", gold: "#B86AE8", bright: "#DA9AFF", dim: "#5E3480", light: "#EEC8FF", win: "#4AE8C4", winDk: "#1E7A64", loss: "#E8724A", lossDk: "#7A3A1E", txt: "#E4DCF4", sub: "#8A7AA0", muted: "#5A4A70", dimTxt: "#302440", panelBg: "rgba(184,106,232,0.04)", panelBorder: "rgba(184,106,232,0.08)" },
    displayFont: "Exo+2", monoFont: "Fira+Code", borderRadius: 10, panelStyle: "glass", dividerStyle: "gradient", cornerStyle: "none", heroStyle: "rainbow", overlays: ["starfield"], vignette: "none", badgeShape: "rounded", iconShape: "rounded", statusHeight: "4", heroSize: 48, labelStyle: "uppercase", statusBarInset: false,
  },
  tactical: {
    name: "Tactical", emoji: "🎖️",
    colors: { bg: "#080A08", gold: "#6AAE5A", bright: "#8ED47A", dim: "#3A5E32", light: "#B8F0A8", win: "#5AB8E0", winDk: "#2A6080", loss: "#E0785A", lossDk: "#803A22", txt: "#D4DED0", sub: "#788A72", muted: "#4A5A44", dimTxt: "#2A3428", panelBg: "rgba(106,174,90,0.04)", panelBorder: "rgba(106,174,90,0.08)" },
    displayFont: "Chakra+Petch", monoFont: "Roboto+Mono", borderRadius: 0, panelStyle: "leftStripe", dividerStyle: "gradient", cornerStyle: "crosshair", heroStyle: "glow", overlays: ["crtLines"], vignette: "none", badgeShape: "square", iconShape: "square", statusHeight: "3", heroSize: 48, labelStyle: "uppercase", statusBarInset: false,
  },
  speakeasy: {
    name: "Speakeasy", emoji: "🥃",
    colors: { bg: "#0B0907", gold: "#C49A5A", bright: "#E8BE7A", dim: "#6E5430", light: "#FFE0AA", win: "#5AC48A", winDk: "#2A6E48", loss: "#C45A6A", lossDk: "#6E2A34", txt: "#EAE0D0", sub: "#908068", muted: "#6A5A44", dimTxt: "#3A3020", panelBg: "rgba(196,154,90,0.04)", panelBorder: "rgba(196,154,90,0.08)" },
    displayFont: "Lora", monoFont: "Inconsolata", borderRadius: 6, panelStyle: "bottomAccent", dividerStyle: "doubleLine", cornerStyle: "deco", heroStyle: "engraved", overlays: ["heavyGrain"], vignette: "warm", badgeShape: "rounded", iconShape: "rounded", statusHeight: "3", heroSize: 48, labelStyle: "uppercase", statusBarInset: true,
  },

  // ═══════════════════════════════════════════════════════════════
  // MIDNIGHT PROTOCOL — Cyberpunk command-line aesthetic
  // ═══════════════════════════════════════════════════════════════
  midnight_terminal: {
    name: "Midnight Terminal", emoji: "💻",
    colors: { bg: "#060A06", gold: "#33E066", bright: "#66FF99", dim: "#1A7A33", light: "#99FFBB", win: "#33BBEE", winDk: "#1A6680", loss: "#E84D4D", lossDk: "#801A1A", txt: "#D0E8D4", sub: "#5A8A60", muted: "#337A40", dimTxt: "#1A3A1E", panelBg: "rgba(51,224,102,0.03)", panelBorder: "rgba(51,224,102,0.08)" },
    displayFont: "Share+Tech+Mono", monoFont: "Fira+Code", borderRadius: 0, panelStyle: "leftStripe", dividerStyle: "leftHeavy", cornerStyle: "crosshair", heroStyle: "glow", overlays: ["crtLines", "scanlines"], vignette: "none", badgeShape: "square", iconShape: "square", statusHeight: "2", heroSize: 44, labelStyle: "uppercase", statusBarInset: false,
  },
  midnight_neon_grid: {
    name: "Midnight Neon Grid", emoji: "🌐",
    colors: { bg: "#04080E", gold: "#00D4E8", bright: "#44F0FF", dim: "#0A6A78", light: "#88FFFF", win: "#A8E84D", winDk: "#5A7A22", loss: "#E85AA0", lossDk: "#7A2254", txt: "#CCE8F0", sub: "#5A8A94", muted: "#2A5A64", dimTxt: "#142A30", panelBg: "rgba(0,212,232,0.04)", panelBorder: "rgba(0,212,232,0.10)" },
    displayFont: "Oxanium", monoFont: "Space+Mono", borderRadius: 2, panelStyle: "glass", dividerStyle: "radial", cornerStyle: "hud", heroStyle: "diagGradient", overlays: ["hexgrid", "sonar"], vignette: "cool", badgeShape: "rounded", iconShape: "rounded", statusHeight: "3", heroSize: 48, labelStyle: "uppercase", statusBarInset: false,
  },
  midnight_glitch: {
    name: "Midnight Glitch", emoji: "📡",
    colors: { bg: "#0A060C", gold: "#E84DCC", bright: "#FF80E6", dim: "#7A2266", light: "#FFAAEE", win: "#4DE8A0", winDk: "#227A50", loss: "#E8CC4D", lossDk: "#7A6E22", txt: "#E8D8EE", sub: "#8A6A90", muted: "#5A3A60", dimTxt: "#2E1A34", panelBg: "rgba(232,77,204,0.04)", panelBorder: "rgba(232,77,204,0.08)" },
    displayFont: "Rubik+Glitch", monoFont: "VT323", borderRadius: 1, panelStyle: "inset", dividerStyle: "dots", cornerStyle: "none", heroStyle: "italic", overlays: ["heavyGrain", "scanlines"], vignette: "heavy", badgeShape: "dashed", iconShape: "square", statusHeight: "4", heroSize: 52, labelStyle: "uppercase", statusBarInset: false,
  },

  // ═══════════════════════════════════════════════════════════════
  // PHARAOH'S TOMB — Ancient Egyptian gold-and-stone luxury
  // ═══════════════════════════════════════════════════════════════
  pharaoh_sandstone: {
    name: "Pharaoh Sandstone", emoji: "🏛️",
    colors: { bg: "#0C0A07", gold: "#D4A84A", bright: "#F0CC70", dim: "#7A6028", light: "#FFE8A0", win: "#4ABEA0", winDk: "#226E5A", loss: "#D45A70", lossDk: "#7A2838", txt: "#F0E8D8", sub: "#9A8A70", muted: "#6A5A3A", dimTxt: "#3A2E1E", panelBg: "rgba(212,168,74,0.04)", panelBorder: "rgba(212,168,74,0.08)" },
    displayFont: "Cinzel", monoFont: "Courier+Prime", borderRadius: 4, panelStyle: "bordered", dividerStyle: "crest", cornerStyle: "deco", heroStyle: "engraved", overlays: ["heavyGrain"], vignette: "warm", badgeShape: "square", iconShape: "square", statusHeight: "4", heroSize: 46, labelStyle: "uppercase", statusBarInset: true,
  },
  pharaoh_obsidian: {
    name: "Pharaoh Obsidian", emoji: "⚱️",
    colors: { bg: "#08070A", gold: "#C4944A", bright: "#E8B870", dim: "#6E5028", light: "#FFD8A0", win: "#5AC4D4", winDk: "#2A6E7A", loss: "#D46A5A", lossDk: "#7A3428", txt: "#E8E0D4", sub: "#8A7E6A", muted: "#5A5038", dimTxt: "#2E2818", panelBg: "rgba(196,148,74,0.04)", panelBorder: "rgba(196,148,74,0.10)" },
    displayFont: "Cinzel+Decorative", monoFont: "IBM+Plex+Mono", borderRadius: 2, panelStyle: "bottomAccent", dividerStyle: "ornament", cornerStyle: "none", heroStyle: "vertGradient", overlays: [], vignette: "heavy", badgeShape: "rounded", iconShape: "rounded", statusHeight: "3", heroSize: 44, labelStyle: "uppercase", statusBarInset: false,
  },
  pharaoh_dynasty: {
    name: "Pharaoh Dynasty", emoji: "🪬",
    colors: { bg: "#070810", gold: "#4A7AD4", bright: "#70A0F0", dim: "#28407A", light: "#A0C8FF", win: "#D4A44A", winDk: "#7A5E22", loss: "#D45A8A", lossDk: "#7A2248", txt: "#D8E0F0", sub: "#707A9A", muted: "#3A4460", dimTxt: "#1E2238", panelBg: "rgba(74,122,212,0.04)", panelBorder: "rgba(74,122,212,0.10)" },
    displayFont: "Playfair+Display", monoFont: "DM+Mono", borderRadius: 6, panelStyle: "inset", dividerStyle: "doubleLine", cornerStyle: "deco", heroStyle: "stamp", overlays: ["scanlines"], vignette: "none", badgeShape: "pill", iconShape: "circle", statusHeight: "6", heroSize: 50, labelStyle: "uppercase", statusBarInset: true,
  },

  // ═══════════════════════════════════════════════════════════════
  // VAPORWAVE SUNSET — Retro-futuristic pink/purple/orange
  // ═══════════════════════════════════════════════════════════════
  vapor_synthwave: {
    name: "Vapor Synthwave", emoji: "🕹️",
    colors: { bg: "#0A0610", gold: "#FF4DA6", bright: "#FF80C4", dim: "#802260", light: "#FFAADD", win: "#4DE8E8", winDk: "#1A7A7A", loss: "#FFB84D", lossDk: "#806020", txt: "#F0D8EE", sub: "#906A8A", muted: "#603A5A", dimTxt: "#301A2E", panelBg: "rgba(255,77,166,0.04)", panelBorder: "rgba(255,77,166,0.08)" },
    displayFont: "Press+Start+2P", monoFont: "VT323", borderRadius: 0, panelStyle: "bordered", dividerStyle: "gradient", cornerStyle: "none", heroStyle: "rainbow", overlays: ["scanlines"], vignette: "heavy", badgeShape: "square", iconShape: "square", statusHeight: "4", heroSize: 36, labelStyle: "uppercase", statusBarInset: false,
  },
  vapor_miami: {
    name: "Vapor Miami", emoji: "🌴",
    colors: { bg: "#0C0808", gold: "#FF7A4D", bright: "#FFA070", dim: "#804022", light: "#FFCC99", win: "#4DD4A0", winDk: "#227A54", loss: "#D44DCC", lossDk: "#6E227A", txt: "#F0E4DA", sub: "#907A6A", muted: "#604A38", dimTxt: "#302218", panelBg: "rgba(255,122,77,0.04)", panelBorder: "rgba(255,122,77,0.08)" },
    displayFont: "Bebas+Neue", monoFont: "Share+Tech+Mono", borderRadius: 8, panelStyle: "leftStripe", dividerStyle: "gradient", cornerStyle: "none", heroStyle: "diagGradient", overlays: [], vignette: "warm", badgeShape: "pill", iconShape: "rounded", statusHeight: "6", heroSize: 64, labelStyle: "uppercase", statusBarInset: false,
  },
  vapor_retrograde: {
    name: "Vapor Retrograde", emoji: "💜",
    colors: { bg: "#08061A", gold: "#A87AE8", bright: "#C4A0FF", dim: "#543A80", light: "#DDC8FF", win: "#7AE8A0", winDk: "#3A7A50", loss: "#E8A04D", lossDk: "#7A5422", txt: "#E0D8F4", sub: "#7A70A0", muted: "#4A3E6A", dimTxt: "#241E40", panelBg: "rgba(168,122,232,0.04)", panelBorder: "rgba(168,122,232,0.08)" },
    displayFont: "Sora", monoFont: "Martian+Mono", borderRadius: 14, panelStyle: "glass", dividerStyle: "dotPattern", cornerStyle: "none", heroStyle: "glow", overlays: ["starfield"], vignette: "cool", badgeShape: "pill", iconShape: "circle", statusHeight: "3", heroSize: 48, labelStyle: "none", statusBarInset: true,
  },

  // ═══════════════════════════════════════════════════════════════
  // ARCTIC OPERATIONS — Military ice/steel precision
  // ═══════════════════════════════════════════════════════════════
  arctic_frostbite: {
    name: "Arctic Frostbite", emoji: "❄️",
    colors: { bg: "#060A10", gold: "#5AA8E0", bright: "#80C8FF", dim: "#2A5A80", light: "#AAE0FF", win: "#4AE8A0", winDk: "#1E7A50", loss: "#E85A6A", lossDk: "#7A2230", txt: "#D8E4F0", sub: "#6A7E90", muted: "#3A5060", dimTxt: "#1A2A38", panelBg: "rgba(90,168,224,0.04)", panelBorder: "rgba(90,168,224,0.10)" },
    displayFont: "Quantico", monoFont: "Roboto+Mono", borderRadius: 2, panelStyle: "bordered", dividerStyle: "leftHeavy", cornerStyle: "crosshair", heroStyle: "vertGradient", overlays: ["crtLines"], vignette: "none", badgeShape: "square", iconShape: "square", statusHeight: "3", heroSize: 48, labelStyle: "uppercase", statusBarInset: false,
  },
  arctic_whiteout: {
    name: "Arctic Whiteout", emoji: "🌨️",
    colors: { bg: "#080A0C", gold: "#A0B0C0", bright: "#C8D4E0", dim: "#506070", light: "#E0E8F0", win: "#5AE0B0", winDk: "#2A7A5A", loss: "#E07A5A", lossDk: "#7A3A22", txt: "#E0E4E8", sub: "#7A8088", muted: "#4A5058", dimTxt: "#222830", panelBg: "rgba(160,176,192,0.04)", panelBorder: "rgba(160,176,192,0.08)" },
    displayFont: "Barlow+Condensed", monoFont: "Overpass+Mono", borderRadius: 1, panelStyle: "leftStripe", dividerStyle: "gradient", cornerStyle: "hud", heroStyle: "engraved", overlays: [], vignette: "cool", badgeShape: "rounded", iconShape: "rounded", statusHeight: "2", heroSize: 54, labelStyle: "uppercase", statusBarInset: false,
  },
  arctic_permafrost: {
    name: "Arctic Permafrost", emoji: "🧊",
    colors: { bg: "#060C0E", gold: "#4DC4B0", bright: "#70E8D4", dim: "#226E60", light: "#99FFEE", win: "#80A8E8", winDk: "#3A5A80", loss: "#E8704D", lossDk: "#7A3822", txt: "#D4E8E4", sub: "#6A8A84", muted: "#3A5A54", dimTxt: "#1A2E2A", panelBg: "rgba(77,196,176,0.04)", panelBorder: "rgba(77,196,176,0.08)" },
    displayFont: "Saira", monoFont: "Red+Hat+Mono", borderRadius: 4, panelStyle: "inset", dividerStyle: "radial", cornerStyle: "none", heroStyle: "diagGradient", overlays: ["hexgrid", "crtLines"], vignette: "none", badgeShape: "dashed", iconShape: "square", statusHeight: "4", heroSize: 46, labelStyle: "uppercase", statusBarInset: true,
  },

  // ═══════════════════════════════════════════════════════════════
  // DRAGON'S HOARD — Fantasy RPG treasure vault
  // ═══════════════════════════════════════════════════════════════
  dragon_molten: {
    name: "Dragon Molten", emoji: "🐉",
    colors: { bg: "#0E0806", gold: "#E05A2A", bright: "#FF804D", dim: "#7A2E14", light: "#FFAA70", win: "#4DD4A0", winDk: "#1E7A54", loss: "#A04DE0", lossDk: "#5422A0", txt: "#F0E0D4", sub: "#907060", muted: "#604030", dimTxt: "#302018", panelBg: "rgba(224,90,42,0.04)", panelBorder: "rgba(224,90,42,0.08)" },
    displayFont: "MedievalSharp", monoFont: "Cutive+Mono", borderRadius: 6, panelStyle: "bordered", dividerStyle: "ornament", cornerStyle: "deco", heroStyle: "vertGradient", overlays: ["heavyGrain"], vignette: "heavy", badgeShape: "rounded", iconShape: "rounded", statusHeight: "6", heroSize: 52, labelStyle: "uppercase", statusBarInset: false,
  },
  dragon_emerald: {
    name: "Dragon Emerald", emoji: "💎",
    colors: { bg: "#060C08", gold: "#2ABA6A", bright: "#4DE88A", dim: "#146A3A", light: "#80FFAA", win: "#D4A04A", winDk: "#7A5A22", loss: "#D44A7A", lossDk: "#7A2244", txt: "#D4EEE0", sub: "#6A9078", muted: "#3A6048", dimTxt: "#1A3024", panelBg: "rgba(42,186,106,0.04)", panelBorder: "rgba(42,186,106,0.08)" },
    displayFont: "Cormorant+SC", monoFont: "Anonymous+Pro", borderRadius: 10, panelStyle: "minimal", dividerStyle: "dotPattern", cornerStyle: "none", heroStyle: "glow", overlays: [], vignette: "warm", badgeShape: "pill", iconShape: "circle", statusHeight: "3", heroSize: 48, labelStyle: "none", statusBarInset: true,
  },
  dragon_mythril: {
    name: "Dragon Mythril", emoji: "⚔️",
    colors: { bg: "#080810", gold: "#8AA0D4", bright: "#A8C0F0", dim: "#445880", light: "#C8DDFF", win: "#A0D44A", winDk: "#5A7A22", loss: "#D4605A", lossDk: "#7A2E28", txt: "#DEE0F0", sub: "#787E98", muted: "#484E68", dimTxt: "#222638", panelBg: "rgba(138,160,212,0.04)", panelBorder: "rgba(138,160,212,0.10)" },
    displayFont: "Bodoni+Moda", monoFont: "Sometype+Mono", borderRadius: 8, panelStyle: "glass", dividerStyle: "doubleLine", cornerStyle: "hud", heroStyle: "engraved", overlays: ["starfield"], vignette: "cool", badgeShape: "rounded", iconShape: "rounded", statusHeight: "4", heroSize: 46, labelStyle: "uppercase", statusBarInset: false,
  },
};

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return `${parseInt(h.substring(0,2),16)},${parseInt(h.substring(2,4),16)},${parseInt(h.substring(4,6),16)}`;
}

function fontName(v) { return v.replace(/\+/g, " "); }

function ColorInput({ label, value, onChange }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
      <input type="color" value={value.startsWith("#") ? value : "#000000"} onChange={e => onChange(e.target.value)}
        style={{ width: 24, height: 24, border: "none", background: "none", cursor: "pointer", padding: 0 }} />
      <span style={{ fontSize: 11, color: "#999", width: 70, flexShrink: 0 }}>{label}</span>
      <input type="text" value={value} onChange={e => onChange(e.target.value)}
        style={{ flex: 1, background: "#1a1a1e", border: "1px solid #333", borderRadius: 3, padding: "3px 6px", color: "#ddd", fontSize: 11, fontFamily: "monospace" }} />
    </div>
  );
}

function Select({ label, value, onChange, options }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 10, color: "#777", marginBottom: 2, textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <select value={value} onChange={e => onChange(e.target.value)}
        style={{ width: "100%", background: "#1a1a1e", border: "1px solid #333", borderRadius: 4, padding: "5px 6px", color: "#ddd", fontSize: 12 }}>
        {options.map(o => <option key={o.value || o.id || o} value={o.value || o.id || o}>{o.label || o}</option>)}
      </select>
    </div>
  );
}

function CheckboxGroup({ label, options, selected, onChange }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ fontSize: 10, color: "#777", marginBottom: 3, textTransform: "uppercase", letterSpacing: 1 }}>{label}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {options.map(o => {
          const active = selected.includes(o.id);
          return (
            <button key={o.id} onClick={() => onChange(active ? selected.filter(x => x !== o.id) : [...selected, o.id])}
              style={{ padding: "3px 8px", fontSize: 10, borderRadius: 3, border: active ? "1px solid #666" : "1px solid #333",
                background: active ? "#2a2a30" : "#141418", color: active ? "#eee" : "#777", cursor: "pointer" }}>
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Slider({ label, value, min, max, step, onChange, suffix = "" }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 10, color: "#777", textTransform: "uppercase", letterSpacing: 1 }}>{label}</span>
        <span style={{ fontSize: 11, color: "#bbb", fontFamily: "monospace" }}>{value}{suffix}</span>
      </div>
      <input type="range" min={min} max={max} step={step || 1} value={value} onChange={e => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: "#666" }} />
    </div>
  );
}

function CardPreview({ theme, state }) {
  const c = theme.colors;
  const df = fontName(theme.displayFont);
  const mf = fontName(theme.monoFont);
  const br = theme.borderRadius;
  const oc = state.outcome;
  const accent = oc === "win" ? c.win : oc === "loss" ? c.loss : c.gold;
  const accentDk = oc === "win" ? c.winDk : oc === "loss" ? c.lossDk : c.dim;
  const pl = state.payout - state.wager;
  const plColor = pl > 0 ? c.win : pl < 0 ? c.loss : c.gold;
  const plStr = pl > 0 ? `+$${pl.toLocaleString()}` : pl < 0 ? `-$${Math.abs(pl).toLocaleString()}` : "$0";
  const poColor = state.payout > state.wager ? c.win : state.payout < state.wager ? c.loss : c.txt;
  const statusBg = oc !== "active"
    ? `linear-gradient(90deg, ${accentDk}, ${accent}, ${accentDk})`
    : `linear-gradient(90deg, ${c.dim}, ${c.gold}, ${c.bright}, ${c.gold}, ${c.dim})`;

  const heroStyles = {
    vertGradient: { background: `linear-gradient(180deg, ${c.light}, ${c.bright} 40%, ${c.gold} 100%)`, WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", filter: `drop-shadow(0 2px 0 rgba(0,0,0,0.5))` },
    diagGradient: { background: `linear-gradient(135deg, ${c.gold}, ${c.bright} 35%, ${c.light} 50%, ${c.bright} 65%, ${c.gold})`, WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", filter: `drop-shadow(0 0 20px ${c.bright}40)` },
    glow: { color: c.bright, textShadow: `0 0 30px ${c.bright}66, 0 0 60px ${c.gold}33, 0 2px 4px rgba(0,0,0,0.5)` },
    stamp: { color: c.bright, textShadow: `2px 2px 0 ${c.dim}, 0 0 20px ${c.bright}33` },
    engraved: { background: `linear-gradient(180deg, ${c.light} 0%, ${c.bright} 40%, ${c.gold} 70%, ${c.dim} 100%)`, WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", filter: `drop-shadow(0 2px 0 rgba(0,0,0,0.5))` },
    rainbow: { background: `linear-gradient(135deg, ${c.gold}, ${c.bright} 30%, ${c.light} 50%, ${c.win} 70%, ${c.bright})`, WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", filter: `drop-shadow(0 0 24px ${c.bright}44)` },
    italic: { color: c.bright, fontStyle: "italic", textShadow: `0 0 30px ${c.bright}66, 0 4px 8px rgba(0,0,0,0.6), 0 1px 0 ${c.dim}` },
  };

  const panelCss = {
    glass: { background: `${c.panelBg}`, borderRadius: br * 0.8, border: `1px solid ${c.panelBorder}`, boxShadow: `inset 0 1px 0 ${c.bright}08` },
    inset: { background: c.panelBg, borderRadius: br * 0.5, border: `1px solid ${c.panelBorder}`, boxShadow: `inset 0 2px 6px rgba(0,0,0,0.3)` },
    bordered: { background: c.panelBg, borderRadius: br, border: `1px solid ${c.panelBorder}`, borderTop: `1px solid ${c.panelBorder}` },
    leftStripe: { background: c.panelBg, borderRadius: br * 0.3, borderLeft: `3px solid ${c.dim}`, borderTop: `1px solid ${c.panelBorder}` },
    minimal: { background: c.panelBg, borderRadius: br, border: `1px solid ${c.panelBorder}` },
    bottomAccent: { background: c.panelBg, borderRadius: br * 0.3, border: `1px solid ${c.panelBorder}`, borderBottom: `2px solid ${c.panelBorder}` },
  };

  const badgeRadius = { rounded: 8, pill: 20, square: 2, dashed: 4 }[theme.badgeShape] || 8;
  const badgeBorder = theme.badgeShape === "dashed"
    ? `1px dashed rgba(${hexToRgb(accent)},0.3)` : `1px solid rgba(${hexToRgb(accent)},0.3)`;
  const iconRadius = { rounded: 8, circle: "50%", square: 2 }[theme.iconShape] || 8;

  const dividerContent = {
    gradient: <div style={{ height: 1, margin: "0 20px", background: `linear-gradient(90deg, transparent, ${c.dim}, transparent)` }} />,
    ornament: <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 20px" }}><div style={{ flex: 1, height: 1, background: `linear-gradient(90deg, transparent, ${c.dim})` }} /><span style={{ fontSize: 10, color: c.dim, opacity: 0.5 }}>◆</span><div style={{ flex: 1, height: 1, background: `linear-gradient(90deg, ${c.dim}, transparent)` }} /></div>,
    crest: <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "2px 28px" }}><div style={{ flex: 1, height: 1, background: c.dim }} /><span style={{ fontSize: 12, color: c.dim }}>♛</span><div style={{ flex: 1, height: 1, background: c.dim }} /></div>,
    dots: <div style={{ textAlign: "center", padding: "4px 24px", fontFamily: `'${mf}', monospace`, fontSize: 11, color: c.dim, letterSpacing: 6 }}>· · · · · · · · · · · · ·</div>,
    doubleLine: <div style={{ padding: "4px 28px", display: "flex", flexDirection: "column", gap: 3 }}><div style={{ height: 1, background: `linear-gradient(90deg, transparent, ${c.dim}, transparent)` }} /><div style={{ height: 1, background: `linear-gradient(90deg, transparent, ${c.dim}, transparent)` }} /></div>,
    leftHeavy: <div style={{ height: 2, margin: "0 20px", background: `linear-gradient(90deg, ${c.gold}, ${c.dim} 30%, transparent)` }} />,
    radial: <div style={{ height: 1, margin: "0 22px", background: `radial-gradient(ellipse at center, ${c.dim}, transparent 70%)` }} />,
    dotPattern: <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "4px 0" }}>{[0,1,2,3,4].map(i => <span key={i} style={{ width: i===2?4:3, height: i===2?4:3, borderRadius: "50%", background: i===2 ? c.gold : c.dim }} />)}</div>,
  };

  const corners = {
    none: null,
    hud: <>
      <div style={{ position: "absolute", top: 8, left: 8, width: 12, height: 12, borderTop: `1.5px solid ${c.gold}30`, borderLeft: `1.5px solid ${c.gold}30`, zIndex: 5 }} />
      <div style={{ position: "absolute", top: 8, right: 8, width: 12, height: 12, borderTop: `1.5px solid ${c.gold}30`, borderRight: `1.5px solid ${c.gold}30`, zIndex: 5 }} />
      <div style={{ position: "absolute", bottom: 8, left: 8, width: 12, height: 12, borderBottom: `1.5px solid ${c.gold}18`, borderLeft: `1.5px solid ${c.gold}18`, zIndex: 5 }} />
      <div style={{ position: "absolute", bottom: 8, right: 8, width: 12, height: 12, borderBottom: `1.5px solid ${c.gold}18`, borderRight: `1.5px solid ${c.gold}18`, zIndex: 5 }} />
    </>,
    crosshair: <>
      {["top:8px;left:8px", "top:8px;right:8px", "bottom:8px;left:8px", "bottom:8px;right:8px"].map((pos, i) => (
        <div key={i} style={{ position: "absolute", ...Object.fromEntries(pos.split(";").map(p => { const [k,v] = p.split(":"); return [k,v]; })), width: 14, height: 14, zIndex: 5 }}>
          <div style={{ position: "absolute", [i<2?"top":"bottom"]: 0, [i%2===0?"left":"right"]: 0, width: 14, height: 1, background: c.dim }} />
          <div style={{ position: "absolute", [i<2?"top":"bottom"]: 0, [i%2===0?"left":"right"]: 0, width: 1, height: 14, background: c.dim }} />
        </div>
      ))}
    </>,
    deco: <>
      {[{t:6,l:10},{t:6,r:10},{b:6,l:10},{b:6,r:10}].map((pos,i) => (
        <div key={i} style={{ position:"absolute", ...pos, zIndex:5, color: c.dim, fontSize:16, lineHeight:1, transform: i===1?"scaleX(-1)":i===2?"scaleY(-1)":i===3?"scale(-1)":"none" }}>╔</div>
      ))}
    </>,
  };

  const overlayElements = theme.overlays.map(id => {
    if (id === "scanlines") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", background: "repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(255,255,255,0.008) 2px,rgba(255,255,255,0.008) 4px)" }} />;
    if (id === "hexgrid") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", opacity: 0.025, backgroundImage: `url("data:image/svg+xml,%3Csvg width='28' height='49' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M14 0L28 12.25V36.75L14 49 0 36.75V12.25z' fill='none' stroke='${encodeURIComponent(c.gold)}' stroke-width='0.5'/%3E%3C/svg%3E")`, backgroundSize: "28px 49px" }} />;
    if (id === "sonar") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", opacity: 0.02, background: `repeating-radial-gradient(circle at 50% 110%, transparent 0px, transparent 60px, ${c.gold}44 61px, transparent 62px)` }} />;
    if (id === "starfield") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", opacity: 0.4, backgroundImage: `radial-gradient(1px 1px at 20px 30px, ${c.bright}44, transparent), radial-gradient(1px 1px at 80px 60px, ${c.win}33, transparent), radial-gradient(1px 1px at 150px 20px, rgba(255,255,255,0.3), transparent), radial-gradient(1px 1px at 300px 40px, ${c.bright}33, transparent), radial-gradient(1px 1px at 500px 30px, ${c.win}33, transparent), radial-gradient(1px 1px at 600px 70px, ${c.gold}44, transparent)`, backgroundSize: "700px 120px" }} />;
    if (id === "crtLines") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", background: `repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(${hexToRgb(c.gold)},0.015) 2px, rgba(${hexToRgb(c.gold)},0.015) 4px)` }} />;
    if (id === "heavyGrain") return <div key={id} style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", opacity: 0.06, backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='1.2' numOctaves='5' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")` }} />;
    return null;
  });

  const vignetteEl = {
    none: null,
    warm: <div style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", background: "radial-gradient(ellipse at 50% 30%, transparent 40%, rgba(0,0,0,0.35) 100%)" }} />,
    cool: <div style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", background: "radial-gradient(ellipse at 50% 30%, transparent 35%, rgba(0,0,0,0.4) 100%)" }} />,
    heavy: <div style={{ position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", background: "radial-gradient(ellipse at 50% 30%, transparent 30%, rgba(0,0,0,0.55) 100%)" }} />,
  }[theme.vignette];

  const sh = parseInt(theme.statusHeight);

  return (
    <div style={{ position: "relative", width: 700, borderRadius: br, overflow: "hidden", background: c.bg, border: `1px solid ${c.panelBorder}`, fontFamily: `'${df}', sans-serif`, transform: "scale(0.5)", transformOrigin: "top left" }}>
      {/* Status bar */}
      <div style={{ height: sh, width: theme.statusBarInset ? "calc(100% - 40px)" : "100%", margin: theme.statusBarInset ? "0 auto" : 0, marginTop: theme.statusBarInset ? 4 : 0, background: statusBg, borderRadius: theme.statusBarInset ? 1 : 0 }} />

      {/* Noise */}
      <div style={{ position: "absolute", inset: 0, opacity: 0.035, pointerEvents: "none", zIndex: 1, backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")` }} />

      {overlayElements}
      {vignetteEl}
      {corners[theme.cornerStyle]}

      {/* Content */}
      <div style={{ position: "relative", zIndex: 2 }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", padding: "14px 20px 8px" }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 10, flex: 1 }}>
            <div style={{ width: 32, height: 32, borderRadius: iconRadius, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15, background: `rgba(${hexToRgb(c.gold)},0.1)`, border: `1px solid ${c.panelBorder}` }}>{state.icon}</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 20, color: c.txt, letterSpacing: 2, textTransform: theme.labelStyle }}>{state.title}</div>
              <div style={{ fontFamily: `'${mf}', monospace`, fontSize: 10, color: c.sub, letterSpacing: 1, textTransform: "uppercase" }}>{state.subtitle}</div>
              <div style={{ fontFamily: `'${mf}', monospace`, fontSize: 9, color: c.dimTxt }}>TXN #A7F2E9</div>
            </div>
          </div>
          <div style={{ flex: 1, textAlign: "center" }}>
            <span style={{ fontFamily: `'${mf}', monospace`, fontWeight: 700, fontSize: 14, color: c.txt, padding: "3px 12px", background: "rgba(255,255,255,0.05)", borderRadius: badgeRadius }}>{state.player}</span>
            {state.streak && <span style={{ fontFamily: `'${mf}', monospace`, fontSize: 10, color: c.bright, marginLeft: 8 }}>{state.streak}</span>}
          </div>
          <div style={{ flex: 1, textAlign: "right" }}>
            <span style={{ fontFamily: `'${mf}', monospace`, fontWeight: 700, fontSize: 12, padding: "4px 14px", borderRadius: badgeRadius, background: `rgba(${hexToRgb(accent)},0.1)`, border: badgeBorder, color: accent, letterSpacing: 0.5 }}>{state.badge}</span>
          </div>
        </div>

        {/* Hero */}
        <div style={{ textAlign: "center", padding: "20px 20px 14px" }}>
          <div style={{ fontFamily: theme.heroStyle === "italic" || theme.heroStyle === "stamp" ? `'${df}', serif` : `'${mf}', monospace`, fontWeight: 800, fontSize: theme.heroSize, letterSpacing: 3, textTransform: theme.labelStyle, lineHeight: 1.1, ...heroStyles[theme.heroStyle] }}>{state.hero}</div>
          <div style={{ fontFamily: `'${mf}', monospace`, fontSize: 11, color: c.sub, marginTop: 8, letterSpacing: 0.5, fontStyle: theme.heroStyle === "italic" ? "italic" : "normal" }}>{state.heroSub}</div>
        </div>

        {dividerContent[theme.dividerStyle]}

        {/* Data Grid */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6, padding: "12px 20px" }}>
          {[
            { label: "WAGER", value: `$${state.wager.toLocaleString()}`, color: c.txt },
            { label: "PAYOUT", value: `$${state.payout.toLocaleString()}`, color: poColor },
            { label: "P&L", value: plStr, color: plColor },
            { label: "BALANCE", value: `$${state.balance.toLocaleString()}`, color: c.txt },
          ].map((d, i) => (
            <div key={i} style={{ padding: "10px 8px", textAlign: "center", ...panelCss[theme.panelStyle] }}>
              <div style={{ fontWeight: 600, fontSize: 11, color: c.muted, letterSpacing: 1.5, textTransform: "uppercase", marginBottom: 4 }}>{d.label}</div>
              <div style={{ fontFamily: `'${mf}', monospace`, fontWeight: 700, fontSize: 17, color: d.color }}>{d.value}</div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div style={{ textAlign: "center", padding: "10px 20px 14px" }}>
          <span style={{ fontFamily: `'${mf}', monospace`, fontWeight: 700, fontSize: 18 }}>
            <span style={{ color: c.gold }}>Balance:</span>
            <span style={{ color: c.txt }}> ${state.balance.toLocaleString()}</span>
          </span>
        </div>
      </div>

      {/* Theme tag */}
      <div style={{ position: "absolute", bottom: 8, right: 14, fontFamily: `'${mf}', monospace`, fontSize: 8, color: c.dimTxt, letterSpacing: 1, zIndex: 10 }}>
        {theme.emoji} {theme.name.toUpperCase()}
      </div>
    </div>
  );
}

const STATES_DATA = [
  { id: "win", label: "WIN", icon: "🃏", title: "BLACKJACK", subtitle: "FLOW Casino", player: "TheWitt", outcome: "win", badge: "BLACKJACK", hero: "BLACKJACK", heroSub: "Natural 21 — Instant Win", wager: 500, payout: 1250, balance: 14750, streak: "🔥 W5" },
  { id: "loss", label: "LOSS", icon: "🎰", title: "SLOTS", subtitle: "FLOW Casino", player: "TheWitt", outcome: "loss", badge: "NO MATCH", hero: "NO MATCH", heroSub: "Better luck next spin", wager: 200, payout: 0, balance: 9800, streak: "" },
  { id: "active", label: "ACTIVE", icon: "📈", title: "CRASH", subtitle: "FLOW Casino", player: "TheWitt", outcome: "active", badge: "LIVE", hero: "2.47×", heroSub: "Multiplier climbing…", wager: 1000, payout: 2470, balance: 12470, streak: "" },
];

function generateExport(theme) {
  const c = theme.colors;
  const dn = fontName(theme.displayFont);
  const mn = fontName(theme.monoFont);
  const id = theme.name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  return `    "${id}": {
        "label": "${theme.name}",
        "emoji": "${theme.emoji}",
        "display_font": "${dn}",
        "mono_font": "${mn}",
        "border_radius": ${theme.borderRadius},
        "panel_style": "${theme.panelStyle}",
        "divider_style": "${theme.dividerStyle}",
        "corner_style": "${theme.cornerStyle}",
        "hero_style": "${theme.heroStyle}",
        "hero_size": ${theme.heroSize},
        "overlays": ${JSON.stringify(theme.overlays)},
        "vignette": "${theme.vignette}",
        "badge_shape": "${theme.badgeShape}",
        "icon_shape": "${theme.iconShape}",
        "status_height": "${theme.statusHeight}",
        "status_bar_inset": ${theme.statusBarInset ? "True" : "False"},
        "vars": {
            "bg":             "${c.bg}",
            "gold":           "${c.gold}",
            "gold-bright":    "${c.bright}",
            "gold-dim":       "${c.dim}",
            "gold-light":     "${c.light}",
            "win":            "${c.win}",
            "win-dark":       "${c.winDk}",
            "loss":           "${c.loss}",
            "loss-dark":      "${c.lossDk}",
            "text-primary":   "${c.txt}",
            "text-sub":       "${c.sub}",
            "text-muted":     "${c.muted}",
            "text-dim":       "${c.dimTxt}",
            "panel-bg":       "${c.panelBg}",
            "panel-border":   "${c.panelBorder}",
        },
    },`;
}

export default function ThemeStudio() {
  const [theme, setTheme] = useState({ ...DEFAULT_THEME });
  const [activeState, setActiveState] = useState(0);
  const [showExport, setShowExport] = useState(false);
  const [activeSection, setActiveSection] = useState("presets");
  const [fontSearch, setFontSearch] = useState("");
  const [fontCategory, setFontCategory] = useState("all");
  const [customFontInput, setCustomFontInput] = useState("");
  const [customFonts, setCustomFonts] = useState([]);   // {label, value, weights, category:"custom"}
  const [loadedFontUrls, setLoadedFontUrls] = useState(new Set());
  const [fontTarget, setFontTarget] = useState("display"); // "display" or "mono"

  // All display fonts = curated + custom
  const allDisplayFonts = [...GOOGLE_FONTS, ...customFonts.filter(f => f.target !== "mono")];
  const allMonoFonts = [...MONO_FONTS, ...customFonts.filter(f => f.target === "mono")];

  // Load a Google Font dynamically by injecting a <link> tag
  const loadGoogleFont = useCallback((fontValue, weights = "400;500;600;700;800") => {
    const url = `https://fonts.googleapis.com/css2?family=${fontValue}:wght@${weights}&display=swap`;
    if (loadedFontUrls.has(url)) return;
    const el = document.createElement("link");
    el.rel = "stylesheet"; el.href = url;
    document.head.appendChild(el);
    setLoadedFontUrls(prev => new Set([...prev, url]));
  }, [loadedFontUrls]);

  const loadFonts = useCallback(() => {
    const df = theme.displayFont;
    const mf = theme.monoFont;
    const dw = allDisplayFonts.find(f => f.value === df)?.weights || "400;500;600;700;800";
    const mw = allMonoFonts.find(f => f.value === mf)?.weights || "400;500;600;700";
    loadGoogleFont(df, dw);
    loadGoogleFont(mf, mw);
  }, [theme.displayFont, theme.monoFont, allDisplayFonts, allMonoFonts, loadGoogleFont]);

  useEffect(() => { loadFonts(); }, [loadFonts]);

  const update = (path, value) => {
    setTheme(prev => {
      const next = JSON.parse(JSON.stringify(prev));
      const keys = path.split(".");
      let obj = next;
      for (let i = 0; i < keys.length - 1; i++) obj = obj[keys[i]];
      obj[keys[keys.length - 1]] = value;
      return next;
    });
  };

  const loadPreset = (id) => {
    const p = PRESETS[id];
    if (p) setTheme({ ...DEFAULT_THEME, ...JSON.parse(JSON.stringify(p)) });
  };

  const sections = {
    presets: "Presets",
    colors: "Colors",
    typography: "Typography",
    structure: "Structure",
    decoration: "Decoration",
    export: "Export",
  };

  const c = theme.colors;

  return (
    <div style={{ display: "flex", height: "100vh", background: "#0e0e12", color: "#ddd", fontFamily: "'Segoe UI', sans-serif", fontSize: 13 }}>
      {/* Sidebar */}
      <div style={{ width: 300, borderRight: "1px solid #222", display: "flex", flexDirection: "column", flexShrink: 0 }}>
        {/* Section tabs */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 2, padding: "8px 8px 4px", borderBottom: "1px solid #1a1a1e" }}>
          {Object.entries(sections).map(([id, label]) => (
            <button key={id} onClick={() => { setActiveSection(id); if (id === "export") setShowExport(true); else setShowExport(false); }}
              style={{ padding: "4px 8px", fontSize: 10, fontWeight: 600, letterSpacing: 0.5, textTransform: "uppercase",
                background: activeSection === id ? "#2a2a30" : "transparent", color: activeSection === id ? "#eee" : "#666",
                border: "1px solid", borderColor: activeSection === id ? "#444" : "transparent", borderRadius: 4, cursor: "pointer" }}>
              {label}
            </button>
          ))}
        </div>

        {/* Section content */}
        <div style={{ flex: 1, overflow: "auto", padding: 10 }}>
          {activeSection === "presets" && (
            <div>
              <div style={{ fontSize: 10, color: "#777", marginBottom: 6, textTransform: "uppercase", letterSpacing: 1 }}>Load Preset</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4 }}>
                {Object.entries(PRESETS).map(([id, p]) => (
                  <button key={id} onClick={() => loadPreset(id)}
                    style={{ padding: "6px 8px", fontSize: 11, textAlign: "left", background: "#141418", border: "1px solid #2a2a2e", borderRadius: 4, color: "#ccc", cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
                    <span>{p.emoji}</span><span>{p.name}</span>
                  </button>
                ))}
              </div>
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 10, color: "#777", marginBottom: 4, textTransform: "uppercase", letterSpacing: 1 }}>Theme Identity</div>
                <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                  <input value={theme.name} onChange={e => update("name", e.target.value)} placeholder="Theme Name"
                    style={{ flex: 1, background: "#1a1a1e", border: "1px solid #333", borderRadius: 4, padding: "5px 8px", color: "#ddd", fontSize: 12 }} />
                  <input value={theme.emoji} onChange={e => update("emoji", e.target.value)} maxLength={4}
                    style={{ width: 40, background: "#1a1a1e", border: "1px solid #333", borderRadius: 4, padding: "5px 6px", color: "#ddd", fontSize: 14, textAlign: "center" }} />
                </div>
              </div>
            </div>
          )}

          {activeSection === "colors" && (
            <div>
              <div style={{ fontSize: 10, color: "#555", marginBottom: 6, letterSpacing: 1 }}>ACCENT / BRAND</div>
              <ColorInput label="Accent" value={c.gold} onChange={v => update("colors.gold", v)} />
              <ColorInput label="Bright" value={c.bright} onChange={v => update("colors.bright", v)} />
              <ColorInput label="Dim" value={c.dim} onChange={v => update("colors.dim", v)} />
              <ColorInput label="Light" value={c.light} onChange={v => update("colors.light", v)} />
              <div style={{ fontSize: 10, color: "#555", margin: "8px 0 4px", letterSpacing: 1 }}>BACKGROUND</div>
              <ColorInput label="Card BG" value={c.bg} onChange={v => update("colors.bg", v)} />
              <ColorInput label="Panel BG" value={c.panelBg} onChange={v => update("colors.panelBg", v)} />
              <ColorInput label="Border" value={c.panelBorder} onChange={v => update("colors.panelBorder", v)} />
              <div style={{ fontSize: 10, color: "#555", margin: "8px 0 4px", letterSpacing: 1 }}>WIN / LOSS</div>
              <ColorInput label="Win" value={c.win} onChange={v => update("colors.win", v)} />
              <ColorInput label="Win Dark" value={c.winDk} onChange={v => update("colors.winDk", v)} />
              <ColorInput label="Loss" value={c.loss} onChange={v => update("colors.loss", v)} />
              <ColorInput label="Loss Dark" value={c.lossDk} onChange={v => update("colors.lossDk", v)} />
              <div style={{ fontSize: 10, color: "#555", margin: "8px 0 4px", letterSpacing: 1 }}>TEXT</div>
              <ColorInput label="Primary" value={c.txt} onChange={v => update("colors.txt", v)} />
              <ColorInput label="Secondary" value={c.sub} onChange={v => update("colors.sub", v)} />
              <ColorInput label="Muted" value={c.muted} onChange={v => update("colors.muted", v)} />
              <ColorInput label="Dim" value={c.dimTxt} onChange={v => update("colors.dimTxt", v)} />
            </div>
          )}

          {activeSection === "typography" && (() => {
            const targetFonts = fontTarget === "display" ? allDisplayFonts : allMonoFonts;
            const filtered = targetFonts.filter(f => {
              if (fontCategory !== "all" && f.category !== fontCategory) return false;
              if (fontSearch && !f.label.toLowerCase().includes(fontSearch.toLowerCase())) return false;
              return true;
            });
            const currentVal = fontTarget === "display" ? theme.displayFont : theme.monoFont;
            const currentLabel = fontTarget === "display"
              ? (allDisplayFonts.find(f => f.value === currentVal)?.label || fontName(currentVal))
              : (allMonoFonts.find(f => f.value === currentVal)?.label || fontName(currentVal));

            const addCustomFont = () => {
              const raw = customFontInput.trim();
              if (!raw) return;
              const value = raw.replace(/\s+/g, "+");
              const exists = [...allDisplayFonts, ...allMonoFonts].some(f => f.value === value);
              if (!exists) {
                const entry = { label: raw, value, weights: "400;500;600;700;800", category: "custom", target: fontTarget === "mono" ? "mono" : "display" };
                setCustomFonts(prev => [...prev, entry]);
              }
              loadGoogleFont(value, "400;500;600;700;800");
              if (fontTarget === "display") update("displayFont", value);
              else update("monoFont", value);
              setCustomFontInput("");
            };

            return (
            <div>
              {/* Display vs Mono toggle */}
              <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
                {[{id:"display",label:"Display Font"},{id:"mono",label:"Mono Font"}].map(t => (
                  <button key={t.id} onClick={() => { setFontTarget(t.id); setFontSearch(""); setFontCategory("all"); }}
                    style={{ flex: 1, padding: "6px 8px", fontSize: 11, fontWeight: 600,
                      background: fontTarget === t.id ? "#2a2a30" : "#141418",
                      border: fontTarget === t.id ? "1px solid #555" : "1px solid #2a2a2e",
                      borderRadius: 4, color: fontTarget === t.id ? "#eee" : "#777", cursor: "pointer" }}>
                    {t.label}
                  </button>
                ))}
              </div>

              {/* Current selection */}
              <div style={{ background: "#141418", border: "1px solid #2a2a2e", borderRadius: 6, padding: "8px 10px", marginBottom: 8 }}>
                <div style={{ fontSize: 9, color: "#555", letterSpacing: 1, textTransform: "uppercase", marginBottom: 3 }}>CURRENT {fontTarget.toUpperCase()} FONT</div>
                <div style={{ fontFamily: `'${fontName(currentVal)}', ${fontTarget === "mono" ? "monospace" : "sans-serif"}`, fontSize: 16, color: "#eee", fontWeight: 700 }}>
                  {currentLabel}
                </div>
                <div style={{ fontFamily: `'${fontName(currentVal)}', ${fontTarget === "mono" ? "monospace" : "sans-serif"}`, fontSize: 11, color: "#888", marginTop: 2 }}>
                  ABCDEFG abcdefg 0123456789
                </div>
              </div>

              {/* Custom font input */}
              <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 9, color: "#555", letterSpacing: 1, textTransform: "uppercase", marginBottom: 3 }}>
                  ADD CUSTOM GOOGLE FONT
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  <input value={customFontInput} onChange={e => setCustomFontInput(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") addCustomFont(); }}
                    placeholder="e.g. Rubik Wet Paint"
                    style={{ flex: 1, background: "#1a1a1e", border: "1px solid #333", borderRadius: 4, padding: "5px 8px", color: "#ddd", fontSize: 11 }} />
                  <button onClick={addCustomFont}
                    style={{ padding: "5px 10px", background: "#1a2a1a", border: "1px solid #2a4a2a", borderRadius: 4, color: "#8ED47A", fontSize: 11, fontWeight: 600, cursor: "pointer", whiteSpace: "nowrap" }}>
                    + Add
                  </button>
                </div>
                <div style={{ fontSize: 9, color: "#444", marginTop: 3 }}>
                  Type any <a href="https://fonts.google.com" target="_blank" rel="noreferrer" style={{ color: "#666" }}>fonts.google.com</a> family name
                </div>
              </div>

              {/* Search */}
              <input value={fontSearch} onChange={e => setFontSearch(e.target.value)}
                placeholder="Search fonts…"
                style={{ width: "100%", background: "#1a1a1e", border: "1px solid #333", borderRadius: 4, padding: "5px 8px", color: "#ddd", fontSize: 11, marginBottom: 6 }} />

              {/* Category filter */}
              {fontTarget === "display" && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 3, marginBottom: 8 }}>
                  {FONT_CATEGORIES.map(cat => (
                    <button key={cat.id} onClick={() => setFontCategory(cat.id)}
                      style={{ padding: "2px 7px", fontSize: 9, fontWeight: 600, letterSpacing: 0.5,
                        background: fontCategory === cat.id ? "#2a2a30" : "#111114",
                        border: `1px solid ${fontCategory === cat.id ? cat.color + "44" : "#222"}`,
                        borderRadius: 3, color: fontCategory === cat.id ? cat.color : "#555", cursor: "pointer" }}>
                      {cat.label}
                    </button>
                  ))}
                </div>
              )}

              {/* Font list */}
              <div style={{ maxHeight: 260, overflow: "auto", borderRadius: 4, border: "1px solid #1e1e22" }}>
                {filtered.map(f => {
                  const active = f.value === currentVal;
                  return (
                    <button key={f.value} onClick={() => {
                        loadGoogleFont(f.value, f.weights);
                        if (fontTarget === "display") update("displayFont", f.value);
                        else update("monoFont", f.value);
                      }}
                      onMouseEnter={() => loadGoogleFont(f.value, f.weights)}
                      style={{ display: "block", width: "100%", textAlign: "left", padding: "6px 10px",
                        background: active ? "#1a2a1a" : "transparent", border: "none", borderBottom: "1px solid #1a1a1e",
                        cursor: "pointer", color: active ? "#8ED47A" : "#bbb" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                        <span style={{ fontFamily: `'${fontName(f.value)}', ${fontTarget === "mono" ? "monospace" : "sans-serif"}`, fontSize: 13, fontWeight: 700 }}>
                          {f.label}
                        </span>
                        <span style={{ fontSize: 8, color: "#444", textTransform: "uppercase", letterSpacing: 0.5 }}>
                          {f.category || "mono"}
                        </span>
                      </div>
                      <div style={{ fontFamily: `'${fontName(f.value)}', ${fontTarget === "mono" ? "monospace" : "sans-serif"}`, fontSize: 10, color: "#555", marginTop: 1 }}>
                        BLACKJACK 0123 $1,250
                      </div>
                    </button>
                  );
                })}
                {filtered.length === 0 && (
                  <div style={{ padding: 16, textAlign: "center", color: "#444", fontSize: 11 }}>
                    No fonts match. Try a different search or add a custom font above.
                  </div>
                )}
              </div>

              <div style={{ marginTop: 10, borderTop: "1px solid #1e1e22", paddingTop: 8 }}>
                <Slider label="Hero Font Size" value={theme.heroSize} min={32} max={72} onChange={v => update("heroSize", v)} suffix="px" />
                <Select label="Hero Style" value={theme.heroStyle} onChange={v => update("heroStyle", v)} options={HERO_STYLES} />
                <Select label="Label Style" value={theme.labelStyle} onChange={v => update("labelStyle", v)}
                  options={[{ value: "uppercase", label: "UPPERCASE" }, { value: "none", label: "Normal Case" }]} />
              </div>
            </div>
            );
          })()}

          {activeSection === "structure" && (
            <div>
              <Slider label="Border Radius" value={theme.borderRadius} min={0} max={20} onChange={v => update("borderRadius", v)} suffix="px" />
              <Select label="Panel Style" value={theme.panelStyle} onChange={v => update("panelStyle", v)} options={PANEL_STYLES} />
              <Select label="Divider Style" value={theme.dividerStyle} onChange={v => update("dividerStyle", v)} options={DIVIDER_STYLES} />
              <Select label="Badge Shape" value={theme.badgeShape} onChange={v => update("badgeShape", v)} options={BADGE_SHAPES} />
              <Select label="Icon Shape" value={theme.iconShape} onChange={v => update("iconShape", v)} options={ICON_SHAPES} />
              <Select label="Status Bar Height" value={theme.statusHeight} onChange={v => update("statusHeight", v)} options={STATUS_HEIGHTS} />
              <div style={{ marginTop: 6 }}>
                <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}>
                  <input type="checkbox" checked={theme.statusBarInset} onChange={e => update("statusBarInset", e.target.checked)} />
                  <span style={{ color: "#aaa" }}>Inset Status Bar</span>
                </label>
              </div>
            </div>
          )}

          {activeSection === "decoration" && (
            <div>
              <Select label="Corner Style" value={theme.cornerStyle} onChange={v => update("cornerStyle", v)} options={CORNER_STYLES} />
              <Select label="Vignette" value={theme.vignette} onChange={v => update("vignette", v)} options={VIGNETTE_OPTIONS} />
              <CheckboxGroup label="Overlay Textures" options={OVERLAY_OPTIONS} selected={theme.overlays} onChange={v => update("overlays", v)} />
            </div>
          )}

          {activeSection === "export" && (
            <div>
              <div style={{ fontSize: 10, color: "#777", marginBottom: 6, textTransform: "uppercase", letterSpacing: 1 }}>Python Dict for atlas_themes.py</div>
              <pre style={{ background: "#0a0a0e", border: "1px solid #222", borderRadius: 4, padding: 10, fontSize: 10, color: "#aee89a", overflow: "auto", maxHeight: 500, whiteSpace: "pre-wrap", wordBreak: "break-all", fontFamily: "monospace", lineHeight: 1.5 }}>
                {generateExport(theme)}
              </pre>
              <button onClick={() => navigator.clipboard?.writeText(generateExport(theme))}
                style={{ marginTop: 8, width: "100%", padding: "8px 12px", background: "#1a3a1a", border: "1px solid #2a5a2a", borderRadius: 4, color: "#8ED47A", fontSize: 12, fontWeight: 600, cursor: "pointer", letterSpacing: 1 }}>
                COPY TO CLIPBOARD
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Preview area */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* State toggle */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "10px 16px", borderBottom: "1px solid #1a1a1e" }}>
          {STATES_DATA.map((s, i) => (
            <button key={s.id} onClick={() => setActiveState(i)}
              style={{ padding: "5px 16px", fontSize: 11, fontWeight: 700, letterSpacing: 1,
                background: activeState === i ? "#1a1a22" : "transparent",
                border: activeState === i ? `1px solid ${s.id === "win" ? "#34D39944" : s.id === "loss" ? "#FB718544" : "#99999944"}` : "1px solid transparent",
                color: activeState === i ? (s.id === "win" ? "#34D399" : s.id === "loss" ? "#FB7185" : "#aaa") : "#555",
                borderRadius: 4, cursor: "pointer" }}>
              {s.label}
            </button>
          ))}
          <div style={{ marginLeft: "auto", fontSize: 11, color: "#555" }}>
            <span style={{ fontSize: 16 }}>{theme.emoji}</span> {theme.name}
          </div>
        </div>

        {/* Card preview */}
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", overflow: "auto", background: "#08080a" }}>
          <CardPreview theme={theme} state={STATES_DATA[activeState]} />
        </div>
      </div>
    </div>
  );
}
