"""
Wizard commands.

'Tale' mud driver, mudlib and interactive fiction framework
Copyright by Irmen de Jong (irmen@razorvine.net)
"""

from __future__ import absolute_import, print_function, division, unicode_literals
import datetime
import inspect
import functools
import sys
import gc
import os
import platform
import threading
from .decorators import disabled_in_gamemode
from ..errors import SecurityViolation, ParseError, ActionRefused
from .. import base, lang, util
from ..player import Player
from .. import __version__

all_commands = {}
LIBRARY_MODULE_NAME = "tale"


def wizcmd(command, *aliases):
    """
    (Internal) decorator to add the command to the global dictionary of commands, with a privilege check wrapper.
    Note that the wizard command (and the aliases) are prefixed by a '!' to make them stand out from normal commands.
    User code should use @wizcmd from cmds.decorators.
    """
    # NOTE: this shares quite some lines of code with cmds.decorators, be sure to keep them in sync
    command = "!" + command
    aliases = ["!" + alias for alias in aliases]

    def wizcmd2(func):
        func.enable_notify_action = False   # none of the wizard commands should be used with notify_action
        func.is_tale_command_func = True

        @functools.wraps(func)
        def executewizcommand(player, parsed, ctx):
            if not "wizard" in player.privileges:
                raise SecurityViolation("Wizard privilege required for verb " + parsed.verb)
            return func(player, parsed, ctx)

        if command in all_commands:
            raise ValueError("Command defined more than once: " + command)
        argspec = inspect.getargspec(func)
        if argspec.args == ["player", "parsed", "ctx"] and argspec.varargs is None and argspec.keywords is None and argspec.defaults is None:
            func.__doc__ = util.format_docstring(func.__doc__)
            all_commands[command] = executewizcommand
            for alias in aliases:
                if alias in all_commands:
                    raise ValueError("Command defined more than once: " + alias)
                all_commands[alias] = executewizcommand
            return executewizcommand
        else:
            raise SyntaxError("invalid wizcmd function signature for: " + func.__name__)
    return wizcmd2


@wizcmd("ls")
def do_ls(player, parsed, ctx):
    """List the contents of a module path under the library tree (try !ls .items.basic)"""
    p = player.tell
    if not parsed.args:
        raise ParseError("ls what path?")
    path = parsed.args[0]
    if not path.startswith("."):
        raise ActionRefused("Path must start with '.'")
    try:
        module_name = LIBRARY_MODULE_NAME
        if len(path) > 1:
            module_name += path
        __import__(module_name)
        module = sys.modules[module_name]
    except (ImportError, ValueError):
        raise ActionRefused("There's no module named " + path)
    p("<%s>" % path, end=True)
    m_items = vars(module).items()
    modules = [x[0] for x in m_items if inspect.ismodule(x[1])]
    classes = [x[0] for x in m_items if type(x[1]) is type and issubclass(x[1], base.MudObject)]
    items = [x[0] for x in m_items if isinstance(x[1], base.Item)]
    livings = [x[0] for x in m_items if isinstance(x[1], base.Living)]
    locations = [x[0] for x in m_items if isinstance(x[1], base.Location)]
    if locations:
        p("Locations: " + ", ".join(locations), end=True)
    if livings:
        p("Livings: " + ", ".join(livings), end=True)
    if items:
        p("Items: " + ", ".join(items), end=True)
    if modules:
        p("Submodules: " + ", ".join(modules), end=True)
    if classes:
        p("Classes: " + ", ".join(classes), end=True)


@wizcmd("clone")
def do_clone(player, parsed, ctx):
    """Clone an item or living directly from the room or inventory, or from an object in the module path"""
    if not parsed.args:
        raise ParseError("Clone what?")
    path = parsed.args[0]
    if path.startswith("."):
        # find an item somewhere in a module path
        path, objectname = path.rsplit(".", 1)
        if not objectname:
            raise ActionRefused("Invalid object path")
        try:
            module_name = LIBRARY_MODULE_NAME
            if len(path) > 1:
                module_name += path
            __import__(module_name)
            module = sys.modules[module_name]
            obj = getattr(module, objectname, None)
        except (ImportError, ValueError):
            raise ActionRefused("There's no module named " + path)
    elif parsed.who_order:
        obj = parsed.who_order[0]
    else:
        raise ActionRefused("Object not found")
    obj.wiz_clone(player)  # actually clone it


@wizcmd("destroy")
def do_destroy(player, parsed, ctx):
    """Destroys an object or creature."""
    if not parsed.who_order:
        raise ParseError("Destroy what or who?")
    if parsed.unrecognized:
        raise ParseError("It's not clear what you mean by: " + ",".join(parsed.unrecognized))
    for victim in parsed.who_info:
        if not util.input_confirm("Are you sure you want to destroy %s?" % victim.title, player):
            continue
        victim.wiz_destroy(player, ctx)  # actually destroy it
        player.tell("You destroyed %r." % victim)
        player.tell_others("{Title} makes some gestures and a tiny black hole appears.\n"
                           "%s disappears in it, and the black hole immediately vanishes." % lang.capital(victim.title))


@wizcmd("clean")
def do_clean(player, parsed, ctx):
    """Destroys all objects contained in something or someones inventory, or the current location (.)"""
    p = player.tell
    if parsed.args and parsed.args[0] == '.':
        # clean the current location
        p("Cleaning the stuff in your environment.")
        player.tell_others("{Title} cleans out the environment.")
        for item in set(player.location.items):
            player.location.remove(item, player)
            item.destroy(ctx)
        for living in set(player.location.livings):
            if not isinstance(living, Player):
                player.location.remove(living, player)
                living.destroy(ctx)
        if player.location.items:
            p("Some items refused to be destroyed!")
    else:
        if len(parsed.who_order) != 1:
            raise ParseError("Clean what or who?")
        victim = parsed.who_order[0]
        if util.input_confirm("Are you sure you want to clean out %s?" % victim.title, player):
            p("Cleaning inventory of", victim)
            player.tell_others("{Title} cleans out the inventory of %s." % victim.title)
            items = victim.inventory
            for item in items:
                victim.remove(item, player)
                item.destroy(ctx)
                p("destroyed", item)
            if victim.inventory_size:
                p("Some items refused to be destroyed!")


@wizcmd("pdb")
@disabled_in_gamemode("mud")
def do_pdb(player, parsed, ctx):
    """Starts a Python debugging session. (Only available in IF mode)"""
    import pdb
    pdb.set_trace()


@wizcmd("wiretap")
def do_wiretap(player, parsed, ctx):
    """Adds a wiretap to something to overhear the messages they receive.
'wiretap .' taps the room, 'wiretap name' taps a creature with that name,
'wiretap -clear' gets rid of all taps."""
    if not parsed.args:
        raise ActionRefused("Wiretap who?")
    arg = parsed.args[0]
    if arg == ".":
        player.create_wiretap(player.location)
        player.tell("Wiretapped room '<location>%s</>'." % player.location.name)
    elif arg == "-clear":
        player.clear_wiretaps()
        player.tell("All wiretaps removed.")
    elif parsed.who_order:
        for living in parsed.who_order:
            if living is player:
                raise ActionRefused("Can't wiretap yourself.")
            if isinstance(living, base.Item):
                raise ActionRefused("Can't wiretap an item, try a living being or a location instead.")
            player.create_wiretap(living)
            player.tell("Wiretapped <living>%s</>." % living.name)
    else:
        raise ActionRefused("Wiretap who?")


@wizcmd("teleport", "teleport_to")
def do_teleport(player, parsed, ctx):
    """Teleport to a location or creature, or teleport a creature to you.
'teleport[_to] .module.path.to.object' teleports [to] that object (location or creature).
'teleport[_to] playername' teleports [to] that player.
'teleport_to @start' teleports you to the starting location for wizards."""
    if not parsed.args:
        raise ActionRefused("Teleport what to where?")
    args = parsed.args
    teleport_self = parsed.verb == "teleport_to"
    if args[0].startswith("."):
        # teleport the wizard to a location somewhere in a module path
        path, objectname = args[0].rsplit(".", 1)
        if not objectname:
            raise ActionRefused("Invalid object path")
        try:
            module_name = LIBRARY_MODULE_NAME
            if len(path) > 1:
                module_name += path
            __import__(module_name)
            module = sys.modules[module_name]
        except (ImportError, ValueError):
            raise ActionRefused("There's no module named " + path)
        target = getattr(module, objectname, None)
        if not target:
            raise ActionRefused("Object not found")
        if teleport_self:
            if isinstance(target, base.Living):
                target = target.location  # teleport to target living's location
            if not isinstance(target, base.Location):
                raise ActionRefused("Can't determine location to teleport to.")
            teleport_to(player, target)
        else:
            if isinstance(target, base.Location):
                raise ActionRefused("Can't teleport a room here, maybe you wanted to teleport TO somewhere?")
            teleport_someone_to_player(target, player)
    else:
        # target is a player (or @start - the wizard starting location)
        if args[0] == "@start":
            teleport_to(player, ctx.config.startlocation_wizard)
        else:
            target = ctx.driver.search_player(args[0])
            if not target:
                raise ActionRefused("%s isn't here." % args[0])
            if teleport_self:
                teleport_to(player, target.location)
            else:
                teleport_someone_to_player(target, player)


def teleport_to(player, location):
    """helper function for teleport command, to teleport the player somewhere"""
    player.tell_others("{Title} makes some gestures and a portal suddenly opens.")
    player.tell_others("%s jumps into the portal, which quickly closes behind %s." % (lang.capital(player.subjective), player.objective))
    player.teleported_from = player.location  # used for the 'return' command
    player.move(location, silent=True)
    player.tell("You've been teleported.")
    player.look()
    location.tell("Suddenly, a shimmering portal opens!", exclude_living=player)
    location.tell("%s jumps out, and the portal quickly closes behind %s." %
                  (lang.capital(player.title), player.objective), exclude_living=player)


def teleport_someone_to_player(who, player):
    """helper function for teleport command, to teleport someone to the player"""
    who.location.tell("Suddenly, a shimmering portal opens!")
    room_msg = "%s is sucked into it, and the portal quickly closes behind %s." % (lang.capital(who.title), who.objective)
    who.location.tell(room_msg, specific_targets=[who], specific_target_msg="You are sucked into it!")
    who.teleported_from = who.location  # used for the 'return' command
    who.move(player.location, silent=True)
    player.location.tell("%s makes some gestures and a portal suddenly opens." % lang.capital(player.title), exclude_living=who)
    player.location.tell("%s tumbles out of it, and the portal quickly closes again." % lang.capital(who.title), exclude_living=who)


@wizcmd("return")
def do_return(player, parsed, ctx):
    """Return a player to the location where they were before a teleport."""
    if len(parsed.who_order) == 1:
        who = parsed.who_order[0]
    elif len(parsed.who_order) == 0:
        who = player
    else:
        raise ActionRefused("You can only return one person at a time.")
    previous_location = getattr(who, "teleported_from", None)
    if previous_location:
        player.tell("Returning <player>%s</> to <location>%s</>" % (who.name, previous_location.name))
        who.location.tell("Suddenly, a shimmering portal opens!")
        room_msg = "%s is sucked into it, and the portal quickly closes behind %s." % (lang.capital(who.title), who.objective)
        who.location.tell(room_msg, specific_targets=[who], specific_target_msg="You are sucked into it!")
        del who.teleported_from
        who.move(previous_location, silent=True)
        who.tell_others("Suddenly, a shimmering portal opens!")
        who.tell_others("{Title} tumbles out of it, and the portal quickly closes again.")
    else:
        player.tell("Can't determine <player>%s</>'s previous location." % who.name)


@wizcmd("reload")
def do_reload(player, parsed, ctx):
    """Reload the given module (Python)."""
    if not parsed.args:
        raise ActionRefused("Reload what?")
    path = parsed.args[0]
    if not path.startswith("."):
        raise ActionRefused("Path must start with '.'")
    try:
        module_name = LIBRARY_MODULE_NAME
        if len(path) > 1:
            module_name += path
        __import__(module_name)
        module = sys.modules[module_name]
    except (ImportError, ValueError):
        raise ActionRefused("There's no module named " + path)
    import imp
    imp.reload(module)
    player.tell("Module has been reloaded:", module.__name__)


@wizcmd("move")
def do_move(player, parsed, ctx):
    """Move something or someone to another location (.), item or creature.
This may work around possible restrictions that could prevent stuff
to be moved around normally. For instance you could use it to pick up
items that are normally fixed in place (move item to playername)."""
    if len(parsed.args) != 2 or len(parsed.who_order) < 1:
        raise ActionRefused("Move what where?")
    thing = parsed.who_order[0]
    if isinstance(thing, base.Living):
        raise ActionRefused("* use 'teleport' instead to move livings around.")
    if parsed.args[1] == "." and len(parsed.who_order) == 1:
        # current room is the target
        target = player.location
    elif len(parsed.who_order) == 2:
        target = parsed.who_order[1]
    else:
        raise ParseError("It's not clear what you want to move where.")
    if thing is target:
        raise ActionRefused("You can't move things inside themselves.")
    # determine the current container of the object that is being moved
    if thing in player:
        thing_container = player
    elif thing in player.location:
        thing_container = player.location
    else:
        raise ParseError("There seems to be no <item>%s</> here." % thing.name)
    thing.move(target, player)
    player.tell("Moved <item>%s</> from %s to %s." % (thing.name, thing_container.name, target.name))
    player.tell_others("{Title} moved %s into %s." % (thing.title, target.title))


@wizcmd("debug")
def do_debug(player, parsed, ctx):
    """Dumps the internal attribute values of a location (.), item or creature."""
    if not parsed.args:
        raise ParseError("Debug what?")
    name = parsed.args[0]
    if name == ".":
        obj = player.location
    elif parsed.who_order:
        obj = parsed.who_order[0]
    else:
        raise ActionRefused("Can't find %s." % name)
    txt = ["<bright>%r</>" % obj, "Class defined in: " + inspect.getfile(obj.__class__)]
    for varname, value in sorted(vars(obj).items()):
        txt.append("<dim>.</>%s<dim>:</> %r" % (varname, value))
    if obj in ctx.driver.heartbeat_objects:
        txt.append("%s receives heartbeats." % obj.name)
    player.tell(*txt, format=False)


@wizcmd("set")
def do_set(player, parsed, ctx):
    """Set an internal attribute of a location (.), object or creature to a new value.
Usage is: set xxx.fieldname=value (you can use Python literals only)"""
    if not parsed.args:
        raise ParseError("Set what? (usage: set xxx.fieldname=value)")
    args = parsed.args[0].split("=")
    if len(args) != 2:
        raise ParseError("Set what? (usage: set xxx.fieldname=value)")
    name, field = args[0].split(".")
    if name == "":
        obj = player.location
    else:
        obj = player.search_item(name, include_inventory=True, include_location=True)
    if not obj:
        obj = player.location.search_living(name)
    if not obj:
        raise ActionRefused("Can't find %s." % name)
    player.tell(repr(obj), end=True)
    import ast
    value = ast.literal_eval(args[1])
    expected_type = type(getattr(obj, field))
    if expected_type is type(value):
        setattr(obj, field, value)
        player.tell("Field set: %s.%s = %r" % (name, field, value))
    else:
        raise ActionRefused("Data type mismatch, expected %s." % expected_type)


@wizcmd("server")
def do_server(player, parsed, ctx):
    """Dump some server information."""
    driver = ctx.driver
    config = ctx.config
    txt = ["<bright>Server information:</>"]
    realtime = datetime.datetime.now()
    realtime = realtime.replace(microsecond=0)
    uptime = realtime - driver.server_started
    hours, seconds = divmod(uptime.total_seconds(), 3600)
    minutes, seconds = divmod(seconds, 60)
    pyversion = "%d.%d.%d" % sys.version_info[:3]
    sixtyfour = "(%d bits)" % (sys.maxsize.bit_length() + 1)
    if hasattr(platform, "python_implementation"):
        implementation = platform.python_implementation()
    else:
        implementation = "Jython" if os.name == "java" else "???"
    txt.append("Python version: %s %s %s on %s" % (implementation, pyversion, sixtyfour, sys.platform))
    txt.append("Tale library: %s   Game version: %s %s" % (__version__, config.name, config.version))
    txt.append("Real time: %s   Uptime: %d:%02d:%02d" % (realtime, hours, minutes, seconds))
    if config.server_tick_method == "timer":
        txt.append("Game time: %s   (%dx real time)" % (ctx.clock, ctx.clock.times_realtime))
    else:
        txt.append("Game time: %s" % ctx.clock)
    if sys.platform == "cli" or os.name=="java":
        gc_objects = "??"
    else:
        gc_objects = str(len(gc.get_objects()))
    txt.append("Number of GC objects: %s   Number of threads: %s" % (gc_objects, threading.active_count()))
    txt.append("Mode: %s   Players: %d   Heartbeats: %d   Deferreds: %d" % (config.server_mode, len(ctx.driver.all_players()), len(driver.heartbeat_objects), len(driver.deferreds)))
    if config.server_tick_method == "timer":
        avg_loop_duration = sum(driver.server_loop_durations) / len(driver.server_loop_durations)
        txt.append("Server loop tick: %.1f sec   Loop duration: %.2f sec." % (config.server_tick_time, avg_loop_duration))
    elif config.server_tick_method == "command":
        txt.append("Server loop tick: %.1f sec   (command driven)." % config.server_tick_time)
    player.tell(*txt, format=False)


@wizcmd("events")
def do_events(player, parsed, ctx):
    """Dump pending events."""
    driver = ctx.driver
    config = ctx.config
    txt = ["<bright>Pending events overview.</> Server tick is %.1f sec." % config.server_tick_time,
           "Heartbeat objects (%d):" % len(driver.heartbeat_objects)]
    for hb in driver.heartbeat_objects:
        txt.append("  " + str(hb))
    txt.append("")
    txt.append("Deferreds (%d):   (server tick: %.1f sec)" % (len(driver.deferreds), config.server_tick_time))
    txt.append("<monospaced>")
    txt.append("<ul>  due   <dim>|</><ul> function            <dim>|</><ul> owner                  </>")
    for d in driver.deferreds:
        txt.append(("%-7s <dim>|</> %-20s<dim>|</> %s") % (d.when_due(ctx.clock, realtime=True), d.callable, d.owner))
    txt.append("</monospaced>")
    player.tell(*txt, format=False)
