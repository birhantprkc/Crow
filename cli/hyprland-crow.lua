-- Hyprland rules for Crow's window, for a Hyprland that reads a LUA config.
-- The installer copies this to ~/.config/hypr/crow.lua and adds
-- `require("hypr.crow")` to hyprland.lua if it is not already required.
--
-- WHY THERE ARE TWO FILES AND NOT ONE. Hyprland picks its parser from the
-- config it finds: `hyprland.conf` gets the legacy ini parser and
-- `windowrule = ...` lines, `hyprland.lua` gets the Lua one and `hl.window_rule`
-- calls. They are not interchangeable -- measured 2026-09-16 on Hyprland 0.56.2
-- with an Omarchy Lua config: `hyprctl keyword windowrule "float, class:^(crow)$"`
-- answered "keyword can't work with non-legacy parsers. Use eval." So this file
-- is the Lua spelling of cli/hyprland-crow.conf, line for line, and the
-- installer ships whichever matches what the user already has.
--
-- WHY THE CLIENT CANNOT DO THIS ITSELF, and it is not an oversight to fix
-- later. On Wayland a client may not place, size or raise its own toplevel:
-- there are no global coordinates, `gtk_window_move()` is a documented no-op,
-- and `set_keep_above()` does nothing. Measured with `hyprctl clients -j`: a
-- window created with `width=500, height=250` came up as `size=[1261,688]
-- floating=false`, tiled into the layout, and `move(150,150)` changed nothing.
-- The compositor owns the frame; a rule is how you talk to it.
--
-- FLOAT IS THE ONE THAT MATTERS. Crow is a frameless window whose layout has a
-- hard minimum -- 520 px of rail plus 560 px of chat plus 50 px of column
-- chrome, see the comment above `min_size` in cli/crow_gui.py -- and a tiling
-- workspace hands out whatever is left after everything else. Tiled at a third
-- of a screen the mask does not fit and the composer is the first thing to go.
--
-- The class is `crow`, set by `GLib.set_prgname("crow")` in cli/crow_gui.py
-- before the window opens; `Crow` with a capital C is the same window under the
-- `CROW_GDK_BACKEND=x11` escape hatch, where the identity is the X11 WM_CLASS
-- and GTK capitalises res_class. Both spellings, or the rules quietly stop
-- applying on exactly the run somebody is already debugging.
--
-- `decorate = false` and `rounding = 0` because the window draws both itself:
-- it is frameless, the title bar is part of the page, and a second border
-- around a design that already has one reads as a mistake.
hl.window_rule({
  match = { class = "^([Cc]row)$" },
  float = true,
  center = true,
  size = { 1180, 800 },
  decorate = false,
  rounding = 0,
})

-- AND THE DESKTOP DOES NOT SHINE THROUGH IT. Omarchy tags every window
-- `default-opacity` and then paints it at 0.985/0.96 -- which is a nice effect
-- on a terminal and the wrong one here: Crow is frameless, its chrome IS the
-- page, and a chat you can read the wallpaper through stops looking like a
-- program and starts looking like a layer. Its own browsers opt out of the same
-- rule for the same reason. Delete this line if you want the effect back.
hl.window_rule({ match = { class = "^([Cc]row)$" }, tag = "-default-opacity",
                 opacity = "1.0 1.0" })

-- THE BROWSER PANE IS A SECOND TOPLEVEL and carries the same class, so the rule
-- above already catches it -- it floats instead of taking half the workspace.
-- It cannot be glued to the main window (see `_pane` in cli/crow_gui.py); `pin`
-- is the closest thing to "stays with me" that Wayland offers, and it is left
-- commented out because it is a preference rather than a requirement.
-- hl.window_rule({ match = { class = "^([Cc]row)$" }, pin = true })
