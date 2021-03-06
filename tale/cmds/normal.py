"""
Normal player commands.

'Tale' mud driver, mudlib and interactive fiction framework
Copyright by Irmen de Jong (irmen@razorvine.net)
"""

from __future__ import absolute_import, print_function, division, unicode_literals
import inspect
import datetime
import random
import itertools
from .. import lang
from .. import soul
from .. import races
from .. import util
from .. import base
from .. import __version__ as tale_version_string
from ..items.basic import GameClock
from ..errors import ParseError, ActionRefused, SessionExit, RetrySoulVerb, RetryParse
from .decorators import disabled_in_gamemode, disable_notify_action, overrides_soul, no_soul_parse

all_commands = {}
cmds_aliases = {}   # commands -> tuple of aliases
abbreviations = {}   # will be injected


def cmd(command, *aliases):
    """
    (Internal) decorator to add the command to the global dictionary of commands.
    User code should use @cmd from cmds.decorators.
    """
    # NOTE: this shares quite some lines of code with cmds.decorators, be sure to keep them in sync
    def cmd2(func):
        if command in all_commands:
            raise ValueError("command defined more than once: " + command)
        argspec = inspect.getargspec(func)
        if argspec.args == ["player", "parsed", "ctx"] and argspec.varargs is None and argspec.keywords is None and argspec.defaults is None:
            func.__doc__ = util.format_docstring(func.__doc__)
            func.is_tale_command_func = True
            if not hasattr(func, "enable_notify_action"):
                func.enable_notify_action = True   # by default the normal commands should be passed to notify_action
            all_commands[command] = func
            cmds_aliases[command] = aliases
            for alias in aliases:
                if alias in all_commands:
                    raise ValueError("command defined more than once: " + alias)
                all_commands[alias] = func
            return func
        else:
            raise SyntaxError("invalid cmd function signature for: " + func.__name__)
    return cmd2


@cmd("inventory")
@disable_notify_action
def do_inventory(player, parsed, ctx):
    """Show the items you are carrying."""
    if parsed.who_order and "wizard" in player.privileges:
        # wizards may look at the inventory of everything else
        other = parsed.who_order[0]
        other.show_inventory(player, ctx)
    else:
        inventory = player.inventory
        if inventory:
            player.tell("You are carrying:", end=True)
            for item in inventory:
                player.tell("  <item>%s</>" % item.title, format=False)
        else:
            player.tell("You are carrying nothing.")
        if ctx.config.money_type:
            player.tell("Money in possession: %s." % ctx.driver.moneyfmt.display(player.money, zero_msg="you are broke"))


@cmd("locate", "search", "find")
def do_locate(player, parsed, ctx):
    """Try to locate a specific item, creature or player."""
    p = player.tell
    if not parsed.args:
        raise ParseError("Locate what/who?")
    if len(parsed.args) > 1 or len(parsed.who_order) > 1:
        raise ParseError("Can only search for one thing at a time.")
    name = parsed.args[0]
    p("You look around to see if you can locate %s." % name)
    player.tell_others("{Title} looks around.")
    if parsed.who_order:
        thing = parsed.who_order[0]
        if thing is player:
            p("You are here, in <location>%s</>." % player.location.name)
            return
        if thing.name.lower() != name.lower() and name.lower() in thing.aliases:
            p("<dim>(By %s you probably mean %s.)</>" % (name, thing.name))
        if thing in player.location:
            if isinstance(thing, base.Living):
                p("<living>%s</> is here next to you." % lang.capital(thing.title))
            else:
                util.print_object_location(player, thing, player.location, False)
        elif thing in player:
            util.print_object_location(player, thing, player, False)
        else:
            p("You can't find that.")
    else:
        # The default parser checks inventory and location, but it didn't find anything.
        # Check inside containers in the player's inventory instead.
        item, container = player.locate_item(name, include_inventory=False, include_location=False, include_containers_in_inventory=True)
        if item:
            if item.name.lower() != name.lower() and name.lower() in item.aliases:
                p("<dim>(By %s you probably mean %s.)</>" % (name, item.name))
            util.print_object_location(player, item, container, False)
        else:
            otherplayer = ctx.driver.search_player(name)  # global player search
            if otherplayer:
                player.tell("<player>%s</> is playing, %s is currently in '<location>%s</>'." % (lang.capital(otherplayer.title), otherplayer.subjective, otherplayer.location.name))
            else:
                p("You can't find that.")


@cmd("drop")
def do_drop(player, parsed, ctx):
    """Drop an item (or all items) you are carrying."""
    if not parsed.args:
        raise ParseError("Drop what?")

    def drop_stuff(items, container):
        items = list(items)
        refused = []
        for item in items:
            try:
                item.move(player.location, player, verb="drop")
                if container is not player and container in player:
                    print_item_removal(player, item, container)
            except ActionRefused as x:
                refused.append((item, str(x)))
        for item, message in refused:
            items.remove(item)
            player.tell(message)
        if items:
            items_str = lang.join(lang.a(item.title) for item in items)
            player.tell("You drop <item>%s</>." % items_str)
            player.tell_others("{Title} drops %s." % items_str)
        else:
            player.tell("You didn't drop anything.")

    arg = parsed.args[0]
    if arg == "all":
        if player.inventory_size == 0:
            raise ActionRefused("You're not carrying anything.")
        else:
            if util.input_confirm("Are you sure you want to drop all you are carrying?", player):
                drop_stuff(player.inventory, player)
    else:
        # drop a single item from the inventory (or a container in the inventory)
        if parsed.who_order:
            item = parsed.who_order[0]
            if item in player:
                drop_stuff([item], player)
            else:
                raise ActionRefused("You can't drop that.")
        else:
            item, container = player.locate_item(arg, include_location=False)
            if item:
                if container is not player:
                    util.print_object_location(player, item, container)
                drop_stuff([item], container)
            else:
                raise ActionRefused("You don't have <item>%s</>." % lang.a(arg))


@cmd("empty")
def do_empty(player, parsed, ctx):
    """Remove the contents from an object."""
    if len(parsed.args) != 1:
        raise ParseError("Empty what?")
    if len(parsed.who_order) > 1:
        raise ParseError("Please be more specific, only empty one thing at a time.")
    container = parsed.who_order[0]
    if not isinstance(container, base.Container):
        raise ActionRefused("You can't take anything from <item>%s</>." % container.title)
    if container in player.location:
        # move the contents to the room
        target = player.location
        action = "dropped"
    elif container in player:
        # move the contents to the player's inventory
        target = player
        action = "took"
    else:
        raise ParseError("You can't seem to empty that.")
    items_moved = []
    for item in container.inventory:
        try:
            item.move(target, player)
            items_moved.append(item.title)
        except ActionRefused as x:
            player.tell(str(x))
    if items_moved:
        itemnames = lang.join(items_moved)
        player.tell("You %s: <item>%s</>." % (action, itemnames))
        player.tell_others("{Title} %s: %s." % (action, itemnames))
    else:
        player.tell("You %s nothing." % action)


@cmd("put", "place")
def do_put(player, parsed, ctx):
    """Put an item (or all items) into something else. If you're not carrying the item, you will first pick it up."""
    p = player.tell
    if len(parsed.args) < 2:
        raise ParseError("Put what where?")
    if parsed.args[0] == "all":
        if player.inventory_size == 0:
            raise ActionRefused("You're not carrying anything.")
        if len(parsed.args) != 2:
            raise ParseError("Put what where?")
        what = list(player.inventory)
        where = parsed.who_order[-1]   # last object is where to put the stuff
        if what:
            if not util.input_confirm("Are you sure you want to put everything away?", player):
                return
    elif parsed.unrecognized:
        raise ActionRefused("You don't see %s." % lang.join(parsed.unrecognized))
    else:
        what = parsed.who_order[:-1]
        where = parsed.who_order[-1]
    if isinstance(where, base.Living):
        raise ActionRefused("You can't put stuff in <living>%s</>, try giving it to %s?" % (where.name, where.objective))
    inventory_items = []
    refused = []
    word_before = parsed.who_info[where].previous_word or "in"
    if word_before != "in" and word_before != "into":
        raise ActionRefused("You can't do that.")  # only supports put X in Y
    for item in what:
        if item is where:
            p("You can't put <item>%s</> %s itself." % (item.title, word_before))
            continue
        try:
            if item in player:
                # simply use the item from the player's inventory
                item.move(where, player)
                inventory_items.append(item)
            elif item in player.location:
                # first take the item from the room, then move it to the target location
                item.move(player, player)
                p("You take %s." % item.title)
                player.tell_others("{Title} takes %s." % item.title)
                item.move(where, player)
                p("You put it in the <item>%s</>." % where.name)
                player.tell_others("{Title} puts it in the %s." % where.name)
        except ActionRefused as x:
            refused.append((item, str(x)))
    for item, message in refused:
        p(message)
    if inventory_items:
        items_msg = lang.join(lang.a(item.title) for item in inventory_items)
        player.tell_others("{Title} puts %s in the %s." % (items_msg, where.name))
        p("You put <item>{items}</> in the <item>{where}</>.".format(items=items_msg, where=where.name))


@cmd("combine", "attach", "apply", "install")
def do_combine(player, parsed, ctx):
    """Combine two items you are carrying."""
    if len(parsed.who_info) != 2:
        messages = {
            "combine": "Combine what with what?",
            "attach": "Attach what to what?",
            "apply": "Apply what to what?",
            "install": "Install what on what?"
        }
        raise ParseError(messages[parsed.verb])
    item1, item2 = tuple(parsed.who_info)
    if item1 not in player or item2 not in player:
        raise ActionRefused("You are not carrying both, try to pick them up first.")
    try:
        item2.combine(item1, player)
    except ActionRefused:
        item1.combine(item2, player)


@cmd("loot", "pilfer", "sack")
def do_loot(player, parsed, ctx):
    """Take all things from something or someone else. Keep in mind that stealing and robbing is frowned upon, to say the least."""
    if len(parsed.args) != 1:
        raise ParseError("Loot what?")
    if len(parsed.who_order) > 1:
        raise ParseError("Please be more specific, you can only loot from one thing at a time.")
    raise RetryParse("take all from "+parsed.who_order[0].name)


@cmd("take", "get", "steal", "rob")
def do_take(player, parsed, ctx):
    """Take something (or all things) from something or someone else. Keep in mind that stealing and robbing is frowned upon, to say the least."""
    p = player.tell
    if len(parsed.args) == 0:
        raise ParseError("Take what?")
    if len(parsed.args) == 1:  # take thing|all
        what_names = parsed.args
        where = None
    else:
        if parsed.who_order:
            last_obj = parsed.who_order[-1]
            if parsed.who_info[last_obj].previous_word == "from":
                # take x[,y and z] from something
                what_names = parsed.args[:-1]
                where = last_obj
            else:
                # take x[,y and z]
                what_names = parsed.args
                where = None
        else:
            # take x[,y and z] - unrecognised names
            what_names = parsed.args
            where = None
    if where is player:
        raise ActionRefused("There's no reason to take things from yourself.")
    if isinstance(where, base.Living):
        player.tell_others("{Title} tries to steal things from %s." % where.title)
        if where.aggressive:
            where.start_attack(player)  # stealing stuff is a hostile act!
        raise ActionRefused("You can't just steal stuff from <living>%s</>!" % where.title)
    elif parsed.verb == "steal" or parsed.verb == "rob":
        if where is None:
            raise ActionRefused("Steal what from whom?")
        raise ActionRefused("You can't steal stuff from an object. Try taking it instead.")
    if what_names == ["all"]:   # take ALL the things!
        if where:
            # take all stuff out of some container
            if where in player or where in player.location:
                # take all stuff from a bag that the player is carrying, or from a bag in the room.
                if where.inventory_size > 0:
                    take_stuff(player, where.inventory, where, where.title)
                    return
                else:
                    raise ActionRefused("There's nothing in there.")
            raise ActionRefused("Take what?")
        else:
            # take all stuff out of the room
            if not player.location.items:
                raise ActionRefused("There's nothing here to take.")
            else:
                take_stuff(player, player.location.items, player.location)
                return
    else:   # take one or more specific items
        if where:
            if where in player or where in player.location:
                # take specific items out of some container
                items_by_name = {item.name: item for item in where.inventory}
                items_to_take = []
                for name in what_names:
                    if name in items_by_name:
                        items_to_take.append(items_by_name[name])
                    else:
                        p("There's no %s in there." % name)
                take_stuff(player, items_to_take, where, where.title)
                return
        else:
            # take things from the room
            if parsed.unrecognized:
                p("You don't see %s." % lang.join(parsed.unrecognized))
            livings = [item for item in parsed.who_order if item in player.location.livings]
            for living in livings:
                try_pick_up_living(player, living)
            if not player.location.items:
                raise ActionRefused("There's nothing here to take.")
            else:
                items_to_take = []
                for item in parsed.who_order:
                    if item in player.location.items:
                        items_to_take.append(item)
                    elif isinstance(item, base.Exit):
                        raise ActionRefused("You can't pick that up.")
                    elif item not in player.location.livings:
                        if item in player:
                            p("You've already got it.")
                        else:
                            p("There's no <item>%s</> here." % item.name)
                take_stuff(player, items_to_take, player.location)
                return


def take_stuff(player, items, container, where_str=None):
    """Takes stuff and returns the number of items taken"""
    if not items:
        return 0
    if where_str:
        player_msg = "You take <item>{items}</> from the <item>%s</>." % where_str
        room_msg = "<player>{{Title}}</> takes <item>{items}</> from the <item>%s</>." % where_str
    else:
        player_msg = "You take <item>{items}</>."
        room_msg = "<player>{{Title}}</> takes <item>{items}</>."
    items = list(items)
    refused = []
    for item in items:
        try:
            item.move(player, player, verb="take")
        except ActionRefused as x:
            refused.append((item, str(x)))
    for item, message in refused:
        player.tell(message)
        items.remove(item)
    if items:
        items_str = lang.join(lang.a(item.title) for item in items)
        player.tell(player_msg.format(items=items_str))
        player.tell_others(room_msg.format(items=items_str))
        return len(items)
    else:
        return 0


def try_pick_up_living(player, living):
    p = player.tell
    living_race = races.races[living.race]
    player_race = races.races[player.race]
    if player_race["size"] - living_race["size"] >= 2:
        # @todo: do an agi/str/spd/luck check to see if we can pick it up
        p("Even though {subj}'s small enough, you can't carry {obj} with you.".format(subj=living.subjective, obj=living.objective))
        if living.aggressive:
            p("Trying to pick {0} up wasn't a very good idea, you've made {0} angry!".format(living.objective))
            living.start_attack(player)
    else:
        p("You can't carry {obj} with you, {subj}'s too large.".format(subj=living.subjective, obj=living.objective))


@cmd("throw")
def do_throw(player, parsed, ctx):
    """Throw something you are carrying at someone or something. If you don't have it yet, you will first pick it up."""
    if len(parsed.who_order) != 2:
        raise ParseError("Throw what where?")
    item, where = parsed.who_order[0], parsed.who_order[1]
    if isinstance(item, base.Living):
        raise ActionRefused("You can't throw that.")
    if item in player.location:
        # first take the item from the room
        item.move(player, player, verb="take")
        player.tell("You take <item>%s</>." % item.title)
        player.tell_others("{Title} takes %s." % item.title)
    # throw the item back into the room, missing the target by a hair. Possibly start combat.
    item.move(player.location, player, verb="throw")
    player.tell("You throw the <item>%s</> at %s, missing %s by a hair." % (item.title, where.title, where.objective))
    player.tell_others("{Title} throws the %s at %s, missing %s by a hair." % (item.title, where.title, where.objective))
    if isinstance(where, base.Living) and where.aggressive:
        where.start_attack(player)


@cmd("give")
def do_give(player, parsed, ctx):
    """Give something (or all things) you are carrying to someone else."""
    if len(parsed.args) < 2:
        raise ParseError("Give what to whom?")
    if len(parsed.who_order) == 1:
        # first try if the first one or two words can be interpreted as an amount of money
        if ctx.config.money_type:
            try:
                money = ctx.driver.moneyfmt.parse(parsed.unrecognized)
                return give_money(player, money, parsed.who_order[0], ctx.driver)
            except (ValueError, ParseError):
                pass
    if parsed.unrecognized:
        raise ParseError("You don't have %s." % lang.join(parsed.unrecognized))
    if player.inventory_size == 0:
        raise ActionRefused("You're not carrying anything.")
    # check for "all"
    if "all" in parsed.args:
        if len(parsed.args) != 2:
            raise ParseError("Give all to who?")
        what = player.inventory
        if what:
            if not util.input_confirm("Are you sure you want to give it all away?", player):
                return
        if parsed.args[0] == "all":
            # give all [to] living
            return give_stuff(player, what, parsed.args[1])
        else:
            # give living all
            return give_stuff(player, what, parsed.args[0])

    # give one or more specific items.
    if  len([who for who in parsed.who_order if isinstance(who, base.Living)]) > 1:
        # if there's more than one living, it's not clear who to give stuff to
        raise ActionRefused("It's not clear who you want to give things to.")
    if isinstance(parsed.who_order[0], base.Living):
        # if the first is a living, assume "give living [the] thing(s)"
        what = parsed.who_order[1:]
        return give_stuff(player, what, None, target=parsed.who_order[0])
    elif isinstance(parsed.who_order[-1], base.Living):
        # if the last is a living, assume "give thing(s) [to] living"
        what = parsed.who_order[:-1]
        return give_stuff(player, what, None, target=parsed.who_order[-1])
    else:
        raise ActionRefused("It's not clear who you want to give things to.")


def give_stuff(player, items, target_name, target=None):
    p = player.tell
    if not target:
        target = player.location.search_living(target_name)
    if not target:
        raise ActionRefused("%s isn't here." % target_name)
    if target is player:
        raise ActionRefused("There's no reason to give things to yourself.")
    items = list(items)
    refused = []
    for item in items:
        try:
            item.move(target, player)
        except ActionRefused as x:
            refused.append((item, str(x)))
    for item, message in refused:
        p(message)
        items.remove(item)
    if items:
        items_str = lang.join(lang.a(item.title) for item in items)
        player_str = lang.capital(player.title)
        room_msg = "<player>%s</> gives <item>%s</> to <living>%s</>." % (player_str, items_str, target.title)
        target_msg = "<player>%s</> gives you <item>%s</>." % (player_str, items_str)
        player.location.tell(room_msg, exclude_living=player, specific_targets=[target], specific_target_msg=target_msg)
        p("You give <living>%s</> <item>%s</>." % (target.title, items_str))
    else:
        p("You didn't give <living>%s</> anything." % target.title)


def give_money(player, amount, recipient, driver):
    if not recipient:
        raise ActionRefused("Give it to whom?")
    if not isinstance(recipient, base.Living):
        raise ActionRefused("You can't do that.")
    if recipient is player:
        raise ActionRefused("There's no reason to give it to yourself.")
    if amount <= 0:
        player.tell("You don't give away anything.")
    elif player.money < amount:
        player.tell("You don't have that amount of wealth.")
    else:
        recipient.allow_give_money(player, amount)
        if util.input_confirm("Are you sure you want to give %s away?" % driver.moneyfmt.display(amount), player):
            player.money -= amount
            recipient.money += amount
            player.tell("You gave <living>%s</> %s." % (recipient.title, driver.moneyfmt.display(amount)))
            player.tell_others("{Title} gave %s some money." % recipient.title)


@cmd("help")
@disable_notify_action
def do_help(player, parsed, ctx):
    """Provides some helpful information about different aspects of the game. Also try 'hint' or 'recap'."""
    if parsed.args:
        do_what(player, parsed, ctx)
    else:
        all_verbs = ctx.driver.get_current_verbs()
        verb_help = {}   # verb -> [list of abbrs]
        aliases = frozenset(itertools.chain(*cmds_aliases.values()))
        for verb in all_verbs:
            if verb not in aliases:
                verb_help[verb] = []
        abbrevs = dict(abbreviations)
        for abbr, verb in abbreviations.items():
            if verb in verb_help:
                verb_help[verb].append(abbr)
                del abbrevs[abbr]
        cmds_help = []
        for verb, abbrs in verb_help.items():
            if abbrs:
                verb += "/" + "/".join(abbrs)
            cmds_help.append(verb)
        player.tell("<bright>Available commands:</>")
        player.tell(", ".join(sorted(cmds_help)), end=True)
        player.tell("\n")
        if aliases:
            player.tell("<bright>Synonyms:</> a different word for one of the commands mentioned above. Makes typing a bit more natural sometimes. The synonyms are: ")
            player.tell(", ".join(sorted(aliases)), end=True)
            player.tell("\n")
        player.tell("<bright>Abbreviations:</>")
        player.tell(", ".join(sorted("%s=%s" % (a, v) for a, v in abbrevs.items())), end=True)
        player.tell("\n")
        player.tell("You can get more info about all kinds of stuff by asking 'what is <topic>' (?topic).")
        player.tell("You can get more info about the 'emote' verbs by asking 'what is soul' (?soul).")
        player.tell("To see all possible verbs ask 'what is emotes' (?emotes).", end=True)
        if player.hints.has_hints():
            player.tell("\n")
            player.tell("<bright>Hints:</>")
            player.tell("When you're stuck, you can use the 'hint' command to try to get a clue about what to do next.")


@cmd("look")
@disable_notify_action
def do_look(player, parsed, ctx):
    """Look around to see where you are and what's around you."""
    if parsed.args:
        arg = parsed.args[0]
        # look <direction> is the only thing we support, the rest should be done with examine
        if arg in player.location.exits:
            exit = player.location.exits[arg]
            player.tell(exit.short_description)
            if exit.short_description != exit.description:
                # give a little hint that more information can be gained by examining it
                player.tell("Maybe you should examine it?")
                return
        elif arg in abbreviations and abbreviations[arg] in player.location.exits:
            player.tell(player.location.exits[abbreviations[arg]].short_description)
        else:
            raise ParseError("Maybe you should examine that instead.")
    else:
        player.look(short=False)


@cmd("examine", "inspect")
@disable_notify_action
def do_examine(player, parsed, ctx):
    """Examine something or someone thoroughly."""
    p = player.tell
    living = None
    if parsed.who_info and isinstance(parsed.who_order[0], base.Living):
        living = parsed.who_order[0]
        name = living.name
    if not living:
        if not parsed.args:
            raise ParseError("Examine what or who?")
        remove_is_are_args(parsed.args)
        name = parsed.args[0]
        living = player.location.search_living(name)
    if living:
        if living is player:
            # player examines him/herself
            p("You are <living>%s</>. But you knew that already." % lang.capital(living.title))
            player.tell_others("{Title} is looking at %sself." % living.objective)
            return
        # if "wizard" in player.privileges:
        #     tell(repr(living), end=True)
        if living.name.lower() != name.lower() and name.lower() in living.aliases:
            p("<dim>(By %s you probably meant %s.)</>" % (name, living.name), end=True)
        p("This is <living>%s</>." % living.title)
        if living.description:
            p(living.description)
        race = races.races[living.race]
        if living.race == "human":
            # don't print as much info when dealing with mere humans
            msg = lang.capital("%s speaks %s." % (living.subjective, race["language"]))
            p(msg)
        else:
            p("{subj}'s a {size} {btype} {race}, and speaks {lang}.".format(
                subj=lang.capital(living.subjective),
                size=races.sizes[race["size"]],
                btype=races.bodytypes[race["bodytype"]],
                race=living.race,
                lang=race["language"]
            ))
        return
    item, container = player.locate_item(name)
    if item:
        if item.name.lower() != name.lower() and name.lower() in item.aliases:
            p("<dim>(By %s you probably meant %s.)</>" % (name, item.name))
        if item in player:
            p("You're carrying <item>%s</>." % lang.a(item.title))
        elif container and container in player:
            util.print_object_location(player, item, container)
        else:
            p("You see <item>%s</>." % lang.a(item.title))
        if item.description:
            p(item.description)
        try:
            inventory = item.inventory
        except ActionRefused:
            pass
        else:
            if inventory:
                p("It contains: <item>%s</>." % lang.join(subitem.title for subitem in inventory))
            else:
                p("It's empty.")
    elif name in player.location.exits:
        p("It seems you can go there:")
        p("<exit>"+player.location.exits[name].description+"</>")
    elif name in abbreviations and abbreviations[name] in player.location.exits:
        p("It seems you can go there:")
        p("<exit>"+player.location.exits[abbreviations[name]].description+"</>")
    else:
        raise ActionRefused("%s isn't here." % name)


@cmd("stats")
@disable_notify_action
@disabled_in_gamemode("if")
def do_stats(player, parsed, ctx):
    """Prints the gender, race and stats information of yourself, or another creature or player."""
    if not parsed.args:
        target = player
    elif len(parsed.who_order) == 1:
        target = parsed.who_order[0]
        if not isinstance(target, base.Living):
            raise ActionRefused("That doesn't have stats.")
    else:
        raise ActionRefused("Show stats from who?")
    gender = lang.GENDERS[target.gender]
    living_type = target.__class__.__name__.lower()
    race = races.races[target.race]
    race_size = races.sizes[race["size"]]
    race_bodytype = races.bodytypes[race["bodytype"]]
    player.tell("<living>%s</> (%s) - %s %s %s" % (target.title, target.name, gender, target.race, living_type), end=True)
    player.tell("%s %s, speaks %s, weighs ~%s kg." % (lang.capital(race_size), race_bodytype, race["language"], race["mass"]), end=True)
    if target.aggressive:
        player.tell("%s seems to be aggressive." % lang.capital(target.subjective), end=True)
    player.tell(", ".join("%s<dim>:</>%s" % (s[0], s[1]) for s in sorted(target.stats.items())))


@cmd("tell")
def do_tell(player, parsed, ctx):
    """Pass a message to another player or creature that nobody else can hear. The other player doesn't have to be in the same location as you."""
    if len(parsed.args) < 1:
        raise ActionRefused("Tell whom what?")
    # we can't use parsed.who_order directly, because the message could be directed to a player
    # that is not in the same location (and hence will not appear in parsed.who_order)
    name = parsed.args[0]
    living = player.location.search_living(name)
    if not living:
        living = ctx.driver.search_player(name)   # is there a player around with this name?
        if not living:
            if name == "all":
                raise ActionRefused("You can't tell something to everyone, only to individuals.")
            raise ActionRefused("%s isn't here." % name)
    if living is player:
        player.tell("You're talking to yourself...")
    else:
        unparsed = parsed.unparsed[len(name):].lstrip()
        if unparsed:
            living.tell("<player>%s</> tells you: %s" % (player.name, unparsed))
            player.tell("You told <living>%s</>." % name)
        else:
            player.tell("Tell %s what?" % living.objective)


@cmd("emote")
@disabled_in_gamemode("if")
def do_emote(player, parsed, ctx):
    """Emit a custom 'emote' message literally, such as: 'emote looks stupid.' -> '<player> looks stupid."""
    if not parsed.unparsed:
        raise ParseError("Emote what message?")
    emote_msg = lang.capital(player.title) + " " + parsed.unparsed
    if not parsed.unparsed.endswith(("!", "?", ".")):
        emote_msg += "."
    player.tell("You emote: %s" % emote_msg)
    player.tell_others(emote_msg)


@cmd("yell")
def do_yell(player, parsed, ctx):
    """Yell something. People in nearby locations will also be able to hear you."""
    if not parsed.unparsed:
        raise ActionRefused("Yell what?")
    message = parsed.unparsed
    if not parsed.unparsed.endswith((".", "!", "?")):
        message += "!"
    player.tell("You yell:", message)
    player.tell_others("{Title} yells: %s" % message)
    util.message_nearby_locations(player.location, "Someone nearby is yelling: " + message)  # yell this to adjacent locations as well


@cmd("say")
@no_soul_parse
def do_say(player, parsed, ctx):
    """Say something to people near you."""
    if not parsed.unparsed:
        raise ActionRefused("Say what?")
    message = parsed.unparsed    # this command is marked @no_soul_parse so everything on the cmd line ends up in here
    if not parsed.unparsed.endswith((".", "!", "?")):
        message += "."
    target = ""
    if parsed.who_order:
        possible_target = parsed.who_order[0]
        if parsed.who_info[possible_target].previous_word == "to":
            if parsed.args[0] in (possible_target.name, possible_target.title) or parsed.args[0] in possible_target.aliases:
                target = " to " + possible_target.title
                _, _, message = message.partition(parsed.args[0])
                message = message.lstrip()
    player.tell("You say%s: %s" % (target, message))
    player.tell_others("{Title} says%s: %s" % (target, message))


@cmd("wait")
@disabled_in_gamemode("mud")
@overrides_soul
def do_wait(player, parsed, ctx):
    """
    Let someone know you are waiting for them. Alternatively, you can simply Let time pass.
    For the latter use, you can optionally specify how long you want to wait (in hours, minutes, seconds).
    """
    if "for" in parsed.unrecognized:
        if not parsed.who_info:
            raise ActionRefused("Who exactly do you want to wait for?")
    if parsed.who_order:
        # check if any of the targeted objects is a non-living
        if not all(isinstance(who, base.Living) for who in parsed.who_order):
            raise ActionRefused("You can't wait for something that's not alive.")
        who = lang.join(who.title for who in parsed.who_order)
        player.tell("You wait for %s." % who)
        player.tell_others("{Title} waits for %s." % who)
        return
    if parsed.args:
        if parsed.args[0] in ("till", "until"):
            # wait until an absolute time on the clock
            wait_time = util.parse_time(parsed.args[1:])
            now_dt = ctx.clock.clock
            wait_dt = datetime.datetime.combine(now_dt.date(), wait_time)
            if wait_dt == now_dt:
                raise ActionRefused("It is already that time.")
            if wait_dt < now_dt:
                wait_dt += datetime.timedelta(hours=24)
            duration = wait_dt - now_dt
        else:
            # wait a given duration
            duration = util.parse_duration(parsed.args)
    else:
        duration = datetime.timedelta(minutes=10)
    max_wait_hours = ctx.config.max_wait_hours
    if max_wait_hours == 0:
        raise ActionRefused("It is not possible to wait.")
    if duration.total_seconds() / 3600 > max_wait_hours:
        msg = lang.spell_number(max_wait_hours) + " " + lang.pluralize("hour", max_wait_hours)
        raise ActionRefused("You can't wait more than " + msg + " at once, who knows what might happen in that time?")
    ok, message = ctx.driver.do_wait(duration)
    if ok:
        player.tell("Time passes. You've waited %s." % util.duration_display(duration))
    else:
        player.tell(message)


@cmd("quit", "leave")
@disable_notify_action
def do_quit(player, parsed, ctx):
    """Quit the game."""
    if util.input_confirm("Are you sure you want to quit?", player):
        if ctx.config.server_mode != "mud" and ctx.config.savegames_enabled:
            if util.input_confirm("Would you like to save your progress?", player):
                do_save(player, parsed, ctx)
        player.tell("\n")
        raise SessionExit()
    player.tell("Good, thank you for staying.")


def print_item_removal(player, item, container, print_parentheses=True):
    if print_parentheses:
        player.tell("<dim>(You take the %s from the %s).</>" % (item.name, container.name))
    else:
        player.tell("You take the %s from the %s." % (item.name, container.name))
    player.tell_others("{Title} takes the %s from the %s." % (item.name, container.name))


def remove_is_are_args(args):
    if args:
        if args[0] == "are":
            raise ActionRefused("Be more specific.")
        elif args[0] == "is":
            if len(args) >= 2:
                del args[0]   # skip 'is', but only if more args follow
            else:
                raise ActionRefused("Who do you mean?")


@cmd("who")
@disable_notify_action
def do_who(player, parsed, ctx):
    """Search for all players, a specific player or creature, and shows some information about them."""
    if parsed.args == ["am", "i"]:
        # who am i
        raise RetryParse("examine myself")      # 'who am i' -> 'examine myself')
    if ctx.config.server_mode == "if":
        # in interactive fiction mode, revert to a simple substitute (examine)
        return do_examine(player, parsed, ctx)
    if parsed.args:
        remove_is_are_args(parsed.args)
        name = parsed.args[0].rstrip("?")
        found = False
        otherplayer = ctx.driver.search_player(name)  # global player search
        if otherplayer:
            found = True
            player.tell("<player>%s</> is playing, %s is currently in '<location>%s</>'." % (lang.capital(otherplayer.title), otherplayer.subjective, otherplayer.location.name))
        try:
            do_examine(player, parsed, ctx)
        except ActionRefused:
            pass
        if not found:
            player.tell("Right now, there's nobody here or playing with that name.")
    else:
        # print all players
        player.tell("All players currently in the game:", end=True)
        for player in ctx.driver.all_players():  # list of all players
            player.tell("<player>%s</> (%s): currently in '<location>%s</>'." % (lang.capital(player.name), player.title, player.location.name), end=True)


@cmd("open", "close", "lock", "unlock")
def do_open(player, parsed, ctx):
    """Do something with a door, exit or item, possibly by using something. Example: open door,  unlock chest with key"""
    if len(parsed.args) not in (1, 2) or parsed.unrecognized:
        raise ParseError("%s what? With what?" % lang.capital(parsed.verb))
    if parsed.who_order:
        if isinstance(parsed.who_order[0], base.Living):
            raise ActionRefused("You can't do that with <living>%s</>." % parsed.who_order[0].title)
    what_name = parsed.args[0]
    with_item_name = None
    with_item = None
    if len(parsed.args) == 2:
        with_item_name = parsed.args[1]
    what = player.search_item(what_name, include_inventory=True, include_location=True, include_containers_in_inventory=False)
    if not what:
        if what_name in player.location.exits:
            what = player.location.exits[what_name]
    if what:
        if with_item_name:
            with_item = player.search_item(with_item_name, include_inventory=True, include_location=False, include_containers_in_inventory=False)
            if not with_item:
                raise ActionRefused("You don't have <item>%s</>." % lang.a(with_item_name))
        getattr(what, parsed.verb)(player, with_item)
        # no need to tell the player or the room, because the verb handler already did this
    else:
        raise ActionRefused("You don't see %s." % lang.a(what_name))


@cmd("what")
@disable_notify_action
def do_what(player, parsed, ctx):
    """Tries to answer your question about what something is. The topics range from game commands to location exits to creature and items. For more general help, try the 'help' command first."""
    p = player.tell
    if not parsed.args:
        raise ParseError("What do you mean?")
    if parsed.args[0] == "are" and len(parsed.args) > 2:
        raise ActionRefused("Be more specific.")
    if len(parsed.args) >= 2 and parsed.args[0] in ("is", "are"):
        del parsed.args[0]
    name = parsed.args[0].rstrip("?")
    if not name:
        raise ActionRefused("What do you mean?")
    found = False
    # is it an abbreviation?
    if name in abbreviations:
        name = abbreviations[name]
        p("It's an abbreviation for %s." % name)
    # is it a command?
    all_verbs = ctx.driver.get_current_verbs()
    if name in all_verbs:
        found = True
        doc = all_verbs[name].strip()
        if doc:
            p(doc)
        else:
            p("It is a command that you can use to perform some action.")
    # is it a soul verb?
    if name in soul.VERBS:
        found = True
        parsed = soul.ParseResult(name)
        parsed.who_order = [player]
        _, playermessage, roommessage, _ = player.socialize_parsed(parsed)
        p("It is a soul emote you can do. <dim>%s: %s</>" % (name, playermessage))
        if name in soul.AGGRESSIVE_VERBS:
            p("It might be regarded as offensive to certain people or beings.")
    if name in soul.BODY_PARTS:
        found = True
        parsed = soul.ParseResult("pat", who_order=[player], bodypart=name, message="hi")
        _, playermessage, roommessage, _ = player.socialize_parsed(parsed)
        p("It denotes a body part. <dim>pat myself %s -> %s</>" % (name, playermessage))
    if name in soul.ACTION_QUALIFIERS:
        found = True
        parsed = soul.ParseResult("smile", qualifier=name)
        _, playermessage, roommessage, _ = player.socialize_parsed(parsed)
        p("It is a qualifier for something. <dim>%s smile -> %s</>" % (name, playermessage))
    if name in lang.ADVERBS:
        found = True
        parsed = soul.ParseResult("smile", adverb=name)
        _, playermessage, roommessage, _ = player.socialize_parsed(parsed)
        p("That's an adverb you can use with the soul emote commands.")
        p("<dim>smile %s -> %s</>" % (name, playermessage))
    if name in races.races:
        found = True
        race = races.races[name]
        size_msg = races.sizes[race["size"]]
        body_msg = races.bodytypes[race["bodytype"]]
        lang_msg = race["language"]
        p("That's a race. They're %s, their body type is %s, and they usually speak %s." % (size_msg, body_msg, lang_msg))
    # is it an exit in the current room?
    if name in player.location.exits:
        found = True
        p("It's a possible way to leave your current location: <exit>%s</>" % player.location.exits[name].short_description)
    # is it a npc here?
    living = player.location.search_living(name)
    if living and living.name.lower() != name.lower() and name.lower() in living.aliases:
        p("<dim>(By %s you probably meant %s.)</>" % (name, living.name))
    if living:
        found = True
        if living is player:
            p("That's you.")
        else:
            title = lang.capital(living.title)
            gender = lang.GENDERS[living.gender]
            subj = lang.capital(living.subjective)
            if type(living) is type(player):
                p("<player>%s</> is a %s %s (player). %s's here." % (title, gender, living.race, subj))
            else:
                p("<living>%s</> is a %s %s. %s's here." % (title, gender, living.race, subj))
    # is it an item somewhere?
    item, container = player.locate_item(name, include_inventory=True, include_location=True, include_containers_in_inventory=True)
    if item:
        found = True
        if item.name.lower() != name.lower() and name.lower() in item.aliases:
            p("<dim>(By %s you probably meant %s.)</>" % (name, item.name))
        p("It's an item in your vicinity. You should perhaps try to examine it.")
    if name == "soul":
        # if player is asking about the soul, give some general info
        found = True
        p("Your soul provides a large amount of 'emotes' or 'verbs' that you can perform.")
        p("An emote is a command that you can do to perform something, or tell something.")
        p("They usually are just for socialization or fun and are not normally considered")
        p("considered to be a command to actually do something or interact with things.")
        p("Your soul knows %d emotes. See them all by asking about 'emotes'." % len(soul.VERBS))
        p("Your soul knows %d adverbs. You can use them by their full name, or make" % len(lang.ADVERBS))
        p("a selection by using prefixes (sa/sar/sarcas -> sarcastically).")
        p("\n")
        p("There are all sorts of emote possibilities, for instance:")
        p("\n")
        p("  fail sit zen  ->  You try to sit zen-likely, but fail miserably.", end=True)
        p("  pat max on the back  ->  You pat Max on the back.", end=True)
        p("  reply max sure thing  ->  You reply to Max: sure thing.", end=True)
        p("  die  ->  You fall down and play dead. (others see: XYZ falls, dead.)", end=True)
        p("  slap all  ->  You slap X, Y and Z in the face.", end=True)
        p("  slap all and me  ->  You slap yourself, X, Y and Z in the face.", end=True)
        p("Often you can target a specific bodypart (try 'what is bodyparts' or ?bodyparts).")
        p("It's sometimes also possible to qualify your action to make it mean something else, such as fail ... or pretend...")
        p("(try 'what are qualifiers' or ?qualifiers).", end=True)
    if name == "emotes":
        # if player asks about the emotes, print all soul emote verbs
        found = True
        p("All available soul verbs (emotes):")
        p("\n")
        columns = player.screen_width // 15
        lines = [""] * (len(soul.VERBS) // columns + 1)
        index = 0
        for verb in sorted(soul.VERBS):
            lines[index % len(lines)] += "%-15s" % verb
            index += 1
        p(*lines, format=False)
    if name in ("adverb", "adverbs"):
        found = True
        p("You can use adverbs such as 'happily', 'zen', 'aggressively' with soul emotes.")
        p("Your soul knows %d adverbs. You can use them by their full name, or make" % len(lang.ADVERBS))
        p("a selection by using prefixes (sa/sar/sarcas -> sarcastically).")
    if name in ("bodypart", "bodyparts"):
        found = True
        p("You can sometimes use a specific body part with certain soul emotes.")
        p("For instance, 'hit max knee' -> You hit Max on the knee.")
        p("Recognised body parts:", ", ".join(soul.BODY_PARTS))
    if name in ("qualifier", "qualifiers"):
        found = True
        p("You can use an action qualifier to change the meaning of a soul emote.")
        p("For instance, 'fail stand' -> You try to stand up, but fail miserably.")
        p("Recognised qualifiers:", ", ".join(soul.ACTION_QUALIFIERS))
    if name in ("that", "this", "they", "them", "it"):
        raise ActionRefused("Be more specific.")
    if not found:
        # too bad, no help available
        p("Sorry, there is no information available about that.")
        if "wizard" in player.privileges:
            p("Maybe you meant to type a wizard command like '!%s'?" % name)


@cmd("exits")
@disable_notify_action
def do_exits(player, parsed, ctx):
    """Provides a tiny clue about possible exits from your current location."""
    if "wizard" in player.privileges:
        player.tell("The following exits are defined for your current location:", end=True)
        for direction, exit in player.location.exits.items():
            if exit.bound:
                player.tell("Exit: <exit>%s</> <dim>-></> <location>%s</>" % (direction, exit.target.name), end=True)
            else:
                player.tell("Exit: <exit>%s</> <dim>-></> <location>%s</> (unbound)" % (direction, exit.target), end=True)
    else:
        player.tell("If you want to know about the possible exits from your location,")
        player.tell("look around the room. Usually the exits are easily visible.")
        if len(player.location.exits) == 1:
            player.tell("Your current location seems to have a possible exit.")
        elif len(player.location.exits) > 1:
            player.tell("Your current location seems to have some possible exits.")
        else:
            player.tell("Your current location doesn't seem to have any obvious exits.")


@cmd("use")
def do_use(player, parsed, ctx):
    """General object use. Most of the time, you'll need to be more specific to say exactly what you want to do with it."""
    if not parsed.who_order:
        raise ActionRefused("Use what?")
    if len(parsed.who_order) > 1:
        # check if there are exactly 2 items mentioned that the player is carrying, assume 'combine' in that case
        if len(parsed.who_info)==2:
            item1, item2 = tuple(parsed.who_info)
            if item1 in player and item2 in player:
                player.tell("<dim>(It is assumed that you want to combine them.)</>")
                return do_combine(player, parsed, ctx)
        subj = "them"
    else:
        who = parsed.who_order[0]
        if isinstance(who, base.Living):
            if who is player:
                raise ActionRefused("Please be more specific: what do you want to do?")
            subj = who.objective
        else:
            subj = "it"
    raise ActionRefused("Please be more specific: what do you want to do with %s?" % subj)


@cmd("dice", "roll")
def do_dice(player, parsed, ctx):
    """Roll a 6-sided die. Use the familiar '3d6' argument style if you want to roll multiple dice."""
    if not parsed.args:
        if parsed.verb == "roll":
            raise RetrySoulVerb
        number = 1
        sides = 6
    else:
        try:
            n, _, s = parsed.args[0].partition("d")
            number, sides = int(n), int(s)
        except ValueError:
            raise ActionRefused("What kind of dice do you want to roll (such as 3d6)?")
    if not (1 <= number <= 20 and sides >= 2):
        raise ActionRefused("Please try a bit more sensible values.")
    total, values = util.roll_die(number, sides)
    die = "a die"
    if (number, sides) != (1, 6):
        die = "%dd%d" % (number, sides)
    player.tell("You roll %s. The result is: %d." % (die, total))
    player.tell_others("{Title} rolls %s. The result is: %d." % (die, total))
    if number > 1:
        player.location.tell("The individual rolls were: %s" % values)


@cmd("coin")
def do_coin(player, parsed, ctx):
    """Toss a coin."""
    number, _ = util.roll_die(sides=2)
    result = ["heads", "tails"][number - 1]
    player.tell("You toss a coin. The result is: %s!" % result)
    player.tell_others("{Title} tosses a coin. The result is: %s!" % result)


@cmd("motd")
@disable_notify_action
@disabled_in_gamemode("if")
def do_motd(player, parsed, ctx):
    """Show the message-of-the-day again."""
    motd, mtime = util.get_motd(ctx.driver.vfs)
    if motd:
        player.tell("<bright>Message-of-the-day, last modified on %s:</>" % mtime, end=True)
        player.tell("\n")
        player.tell(motd)
    else:
        player.tell("There's currently no message-of-the-day.")


@cmd("flee")
def do_flee(player, parsed, ctx):
    """Flee in a random or given direction, possibly escaping a combat situation."""
    exit = None
    if len(parsed.who_order) == 1:
        exit = parsed.who_order[0]
        if not isinstance(exit, base.Exit):
            raise ParseError("You can't flee there.")
        exit.allow_passage(player)
    elif parsed.args:
        raise ParseError("Flee where?")
    random_direction = not exit
    if random_direction:
        # choose a random exit direction
        if not player.location.exits:
            raise ActionRefused("You can't flee anywhere!")
        exit = random.choice(list(player.location.exits.values()))
    exits_to_try = list(player.location.exits.values())
    exits_to_try.insert(0, exit)
    for exit in exits_to_try:
        try:
            exit.allow_passage(player)
            player.tell("You flee in a random direction!" if random_direction else "You flee!", end=True)
            player.tell("\n")
            # @todo stop combat
            player.move(exit.target)
            player.look()
            return
        except ActionRefused:
            pass
    raise ActionRefused("You can't flee anywhere!")


@cmd("save")
@disable_notify_action
@disabled_in_gamemode("mud")
def do_save(player, parsed, ctx):
    """Save your game."""
    ctx.driver.do_save(player)


@cmd("load", "reload", "restore", "restart")
@disable_notify_action
@disabled_in_gamemode("mud")
def do_load(player, parsed, ctx):
    """Load a previously saved game."""
    player.tell("If you want to restart or reload a previously saved game, please quit the game (without saving!)",
                "and start it again. During startup, select the appropriate option to start from a saved game,",
                "or start a new game.")


@cmd("transcript")
@disable_notify_action
@disabled_in_gamemode("mud")
def do_transcript(player, parsed, ctx):
    """Makes a transcript of your game session to the specified file, or switches transcript off again."""
    if parsed.unparsed == "off" or (parsed.args and parsed.args[0] == "off"):
        player.activate_transcript(None, None)
    elif not parsed.args:
        raise ParseError("Transcript to what file? (or off)")
    else:
        player.activate_transcript(parsed.args[0], ctx.driver.vfs)


@cmd("show")
def do_show(player, parsed, ctx):
    """Shows something to someone else."""
    if len(parsed.who_order) != 2:
        raise ParseError("Show what to whom?")
    shown = parsed.who_order[0]
    if shown not in player:
        raise ActionRefused("You don't have <item>%s</>." % lang.a(shown.title))
    target = parsed.who_order[1]
    player.tell("You show the <item>%s</> to <living>%s</>." % (shown.title, target.title))
    room_msg = "%s shows %s to %s." % (lang.capital(player.title), lang.a(shown.title), target.title)
    target_msg = "%s shows you %s." % (lang.capital(player.title), lang.a(shown.title))
    player.location.tell(room_msg, exclude_living=player, specific_target_msg=target_msg, specific_targets=[target])


@cmd("time", "date")
@disable_notify_action
def do_time(player, parsed, ctx):
    """Query the current date and/or time of day."""
    if "wizard" in player.privileges:
        real_time = datetime.datetime.now()
        real_time = real_time.replace(microsecond=0)
        player.tell("The game time is:", ctx.clock)
        player.tell("\n")
        player.tell("Real time is:", real_time)
        return
    if ctx.config.display_gametime:
        for item in player.inventory:
            if isinstance(item, GameClock):
                player.tell("You glance at your %s." % item.name)
                player.tell(item.description)
                return
        raise ActionRefused("You don't have a watch, so you're unsure what %s it is." % parsed.verb)
    raise ActionRefused("You have no idea what %s it is." % parsed.verb)


@cmd("brief")
@disable_notify_action
def do_brief(player, parsed, ctx):
    """Configure the verbosity of location descriptions. 'brief' mode means: show short description
    for locations that you've already visited at least once.
    'brief all' means: show short descriptions for all locations even if you've not been there before.
    'brief off': disable brief mode, always show long descriptions.
    'brief reset': disable brief mode and forget about the known locations as well.
    Note that when you explicitly use the 'look' or 'examine' commands, the brief setting is ignored.
    """
    if parsed.unparsed == "off" or (parsed.args and parsed.args[0] == "off"):
        player.brief = 0
        player.tell("Verbose location descriptions restored.")
    elif not parsed.args:
        player.brief = 1
        player.tell("Brief location descriptions enabled for known locations.")
    elif parsed.args[0] == "all":
        player.brief = 2
        player.tell("Brief location descriptions enabled for all locations.")
    elif parsed.args[0] == "reset":
        player.brief = 0
        count = len(player.known_locations)
        player.known_locations.clear()
        player.tell("Verbose location descriptions have been restored, and you've forgotten about %d previously visited locations." % count)
    else:
        raise ParseError("That's not recognised by this command.")


@cmd("activate")
def do_activate(player, parsed, ctx):
    """Activate something, turn it on, or switch it on."""
    if not parsed.who_order:
        raise ParseError("Activate what?")
    for what in parsed.who_order:
        try:
            what.activate(player)
        except ActionRefused as ex:
            msg = str(ex)
            if len(parsed.who_order) > 1:
                player.tell("%s: %s" % (what.name, msg))
            else:
                player.tell(msg)


@cmd("deactivate")
def do_deactivate(player, parsed, ctx):
    """Deactivate something, turn it of, or switch it off."""
    if not parsed.who_order:
        raise ParseError("Deactivate what?")
    for what in parsed.who_order:
        try:
            what.deactivate(player)
        except ActionRefused as ex:
            msg = str(ex)
            if len(parsed.who_order) > 1:
                player.tell("%s: %s" % (what.name, msg))
            else:
                player.tell(msg)


@cmd("switch")
def do_switch(player, parsed, ctx):
    """Switch something on or off."""
    if len(parsed.who_order) == 1:
        who = parsed.who_order[0]
        if parsed.who_info[who].previous_word == "on" or parsed.unparsed.endswith(" on"):
            do_activate(player, parsed, ctx)
            return
        elif parsed.who_info[who].previous_word == "off" or parsed.unparsed.endswith(" off"):
            do_deactivate(player, parsed, ctx)
            return
    elif len(parsed.who_order) == 0:
        arg = parsed.unparsed.partition(" ")[0]
        if arg in ("on", "off"):
            raise ParseError("Switch %s what?" % arg)
    raise RetrySoulVerb


@cmd("turn")
def do_turn(player, parsed, ctx):
    """Turn something (rotate it), or turn something on or off."""
    if len(parsed.who_order) == 1:
        who = parsed.who_order[0]
        if parsed.who_info[who].previous_word == "on" or parsed.unparsed.endswith(" on"):
            do_activate(player, parsed, ctx)
            return
        elif parsed.who_info[who].previous_word == "off" or parsed.unparsed.endswith(" off"):
            do_deactivate(player, parsed, ctx)
            return
    elif len(parsed.who_order) == 0:
        arg = parsed.unparsed.partition(" ")[0]
        if arg in ("on", "off"):
            raise ParseError("Turn %s what?" % arg)
    # "turn X" -> same as rotate, see below
    do_manipulate(player, parsed, ctx)


@cmd("move", "shove", "swivel", "shift", "manipulate", "manip", "rotate", "press", "poke", "push")
def do_manipulate(player, parsed, ctx):
    """Manipulate something."""
    if parsed.verb == "manip":
        parsed.verb = "manipulate"
    if len(parsed.who_order) == 1:
        what = parsed.who_order[0]
        try:
            what.manipulate(parsed.verb, player)
            return
        except ActionRefused:
            if player.soul.is_verb(parsed.verb):
                raise RetrySoulVerb
            raise
    if player.soul.is_verb(parsed.verb):
        raise RetrySoulVerb  # some of these commands are also soul verbs
    raise ParseError("%s what?" % lang.capital(parsed.verb))


@cmd("read")
def do_read(player, parsed, ctx):
    """Read something."""
    if len(parsed.who_order) == 1:
        what = parsed.who_order[0]
        what.read(player)
    else:
        raise ParseError("Read what?")


@cmd("license")
def do_gameinfo(player, parsed, ctx):
    """Show information about the game and about Tale, and show the software license."""
    t = player.tell
    # version info
    config = ctx.config
    author_addr = " (%s)" % config.author_address if config.author_address else ""
    t("This game, '<bright>%s</>' v%s," % (config.name, config.version))
    t("is written by <bright>%s%s</>," % (config.author, author_addr))
    t("and is using Tale framework v%s." % tale_version_string, end=True)
    t("\n")
    # print GPL 3.0 banner
    t("<bright>Tale: mud driver, mudlib and interactive fiction framework.", end=True)
    t("Copyright (C) 2012  Irmen de Jong.</>", end=True)
    t("This program comes with ABSOLUTELY NO WARRANTY. This is free software,")
    t("and you are welcome to redistribute it under the terms and conditions")
    t("of the GNU General Public License version 3. See the file LICENSE.txt", end=True)


@cmd("config")
def do_config(player, parsed, ctx):
    """Show or change player configuration parameters."""
    config = ctx.config
    driver = ctx.driver
    if parsed.args:
        if len(parsed.args) != 1:
            raise ParseError("Configure what? Usage is: config parameter=value")
        param, _, value = parsed.args[0].partition("=")
        if not value:
            raise ParseError("You must provide a value.")
        if param == "delay":
            value = int(value)
            if 0 <= value <= 100:
                player.io.output_line_delay = value
            else:
                raise ActionRefused("Invalid delay value, range is 0..100")
        elif param == "width":
            value = int(value)
            if 40 <= value <= 200:
                player.screen_width = value
            else:
                raise ActionRefused("Invalid screen width, range is 40..200")
        elif param == "styles":
            player.screen_styles_enabled = value.lower() in ("y", "yes", "true", "enable", "enabled", "on")
        elif param == "smartquotes":
            player.smartquotes_enabled = value.lower() in ("y", "yes", "true", "enable", "enabled", "on")
        else:
            raise ActionRefused("Invalid parameter name.")
        player.tell("Configuration modified.", end=True)
        player.tell("\n")
    player.tell("Configuration:", end=True)
    player.tell("  delay <dim>(output line delay) =</> %d" % player.io.output_line_delay, format=False)
    player.tell("  width <dim>(screen width) =</> %d" % player.screen_width, format=False)
    player.tell("  styles <dim>(enable text styles) =</> %s" % player.screen_styles_enabled, format=False)
    player.tell("  smartquotes <dim>(use typographic quotes) =</> %s" % player.smartquotes_enabled, format=False)


@cmd("hint")
def do_hint(player, parsed, ctx):
    """Provide a clue about what to do next. Also try 'help', and 'recap'."""
    hint = player.hints.hint(player)
    if hint:
        player.tell(hint)
    else:
        player.tell("You're on your own to decide what to do next...")


@cmd("recap")
def do_recap(player, parsed, ctx):
    """
    Shows the key events or actions that have happened so that you might
    get back up to speed with the story so far.
    """
    recapmessages = player.hints.recap()
    if recapmessages:
        for msg in recapmessages:
            player.tell(msg, end=True)
    else:
        player.tell("There's not much to say about the events thus far.")


@cmd("@teststyles")
def do_teststyles(player, parsed, ctx):
    """Test the text output styling (styles and colors)."""
    style_tests = [
        ("normal", "This is NORMAL."),
        ("dim", "<dim>This is DIM.</>"),
        ("bright", "<bright>This is BRIGHT.</>"),
        ("ul", "<ul>This is UNDERLINED.</>"),
        ("rev", "<rev>This is RERVERSE VIDEO.</>"),
        ("blink", "<blink>This is BLINKING.</>"),
        ("black", "<black>This is BLACK.</> (black text)"),
        ("red", "<red>This is RED.</>"),
        ("green", "<green>This is GREEN.</>"),
        ("yellow", "<yellow>This is YELLOW.</>"),
        ("blue", "<blue>This is BLUE.</>"),
        ("magenta", "<magenta>This is MAGENTA.</>"),
        ("cyan", "<cyan>This is CYAN.</>"),
        ("white", "<white>This is WHITE.</> (white text)"),
        ("bg:black", "<bg:black>This is BG:BLACK.</> (black background)"),
        ("bg:red", "<bg:red>This is BG:RED.</>"),
        ("bg:green", "<bg:green>This is BG:GREEN.</>"),
        ("bg:yellow", "<bg:yellow>This is BG:YELLOW.</>"),
        ("bg:blue", "<bg:blue>This is BG:BLUE.</>"),
        ("bg:magenta", "<bg:magenta>This is BG:MAGENTA.</>"),
        ("bg:cyan", "<bg:cyan>This is BG:CYAN.</>"),
        ("bg:white", "<bg:white>This is BG:WHITE.</> (white background)"),
        ("living", "<living>This is LIVING.</>"),
        ("player", "<player>This is PLAYER.</>"),
        ("item", "<item>This is ITEM.</>"),
        ("exit", "<exit>This is EXIT.</>"),
        ("(combined)", "<green><bg:blue><ul><bright>Bright green on blue, underlined. <rev>(reverse video)</>")
    ]
    player.tell("Text style and coloring tests. Depending on the capabilities of the output device,")
    player.tell("you should see various colors and text formatting styles being used.")
    player.tell("If you're on a text console and don't see any colors, make sure the 'colorama' Python module is installed.")
    player.tell("Note that some styles are not widely supported (blink, italic).", end=True)
    for style, example in style_tests:
        player.tell("  %-15s %s" % (style, example), format=False)
