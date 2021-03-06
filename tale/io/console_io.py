"""
Console-based input/output.

'Tale' mud driver, mudlib and interactive fiction framework
Copyright by Irmen de Jong (irmen@razorvine.net)
"""
from __future__ import absolute_import, print_function, division, unicode_literals
import sys
import os
import threading
from . import styleaware_wrapper, iobase
try:
    from . import colorama_patched as colorama
    colorama.init()
except ImportError:
    from . import ansi_codes as colorama        # fallback

if sys.version_info < (3, 0):
    input = raw_input
else:
    input = input

__all__ = ["ConsoleIo"]

style_colors = {
    "dim": colorama.Style.DIM,
    "normal": colorama.Style.NORMAL,
    "bright": colorama.Style.BRIGHT,
    "ul": colorama.Style.UNDERLINED,
    "rev": colorama.Style.REVERSEVID,
    "/": colorama.Style.RESET_ALL,
    "blink": colorama.Style.BLINK,
    "black": colorama.Fore.BLACK,
    "red": colorama.Fore.RED,
    "green": colorama.Fore.GREEN,
    "yellow": colorama.Fore.YELLOW,
    "blue": colorama.Fore.BLUE,
    "magenta": colorama.Fore.MAGENTA,
    "cyan": colorama.Fore.CYAN,
    "white": colorama.Fore.WHITE,
    "bg:black": colorama.Back.BLACK,
    "bg:red": colorama.Back.RED,
    "bg:green": colorama.Back.GREEN,
    "bg:yellow": colorama.Back.YELLOW,
    "bg:blue": colorama.Back.BLUE,
    "bg:magenta": colorama.Back.MAGENTA,
    "bg:cyan": colorama.Back.CYAN,
    "bg:white": colorama.Back.WHITE,
    "living": colorama.Style.BRIGHT,
    "player": colorama.Style.BRIGHT,
    "item": colorama.Style.BRIGHT,
    "exit": colorama.Style.BRIGHT,
    "location": colorama.Style.BRIGHT,
    "monospaced": "",  # we assume the console is already monospaced font
    "/monospaced": ""
}
assert len(set(style_colors.keys()) ^ iobase.ALL_COLOR_TAGS) == 0, "mismatch in list of style tags"

if sys.platform=="win32":
    if not hasattr(colorama, "win32") or colorama.win32.windll is None:
        style_colors.clear()  # running on win32 without colorama ansi support

if sys.platform=="cli" or os.name=="java":
    style_colors.clear()  # IronPython and Jython don't support console colors at all


class ConsoleIo(iobase.IoAdapterBase):
    """
    I/O adapter for the text-console (standard input/standard output).
    """
    def __init__(self, config):
        super(ConsoleIo, self).__init__(config)
        try:
            encoding = getattr(sys.stdout, "encoding", sys.getfilesystemencoding())
            if sys.version_info < (3, 0):
                unichr(8230).encode(encoding)
            else:
                chr(8230).encode(encoding)
        except (UnicodeEncodeError, TypeError):
            self.supports_smartquotes = False
        self.stop_main_loop = False

    def mainloop(self):
        """Main event loop for the console I/O adapter"""
        while not self.stop_main_loop:
            # Input a single line of text by the player. It is stored in the internal
            # command buffer of the player. The driver's main loop can look into that
            # to see if any input should be processed.
            try:
                # note that we don't print any prompt ">>", that needs to be done
                # by the main thread that handles screen *output*
                # (otherwise the prompt will often appear before any regular screen output)
                cmd = input().strip()
                self.player.store_input_line(cmd)
            except KeyboardInterrupt:
                self.break_pressed()
            except EOFError:
                pass

    def clear_screen(self):
        """Clear the screen"""
        if style_colors:
            print("\033[1;1H\033[2J", end="")
        else:
            print("\n" * 5)

    def install_tab_completion(self, completer):
        """Install tab completion using readline, if available"""
        try:
            import readline
            readline.set_completer(completer.complete)
            readline.parse_and_bind("tab: complete")
        except ImportError:
            return

    def abort_all_input(self, player):
        """abort any blocking input, if at all possible"""
        # This requires some drastic measures unfortunately.
        # The main thread is stuck in a blocking input (reading from stdin)
        # You really can't seem to interrupt that. So we terminate the process forcefully.
        player.store_input_line("")
        import signal, os
        os.kill(os.getpid(), signal.SIGINT)

    def render_output(self, paragraphs, **params):
        """
        Render (format) the given paragraphs to a text representation.
        It doesn't output anything to the screen yet; it just returns the text string.
        Any style-tags are still embedded in the text.
        This console-implementation expects 2 extra parameters: "indent" and "width".
        """
        if not paragraphs:
            return None
        indent = " " * params["indent"]
        wrapper = styleaware_wrapper.StyleTagsAwareTextWrapper(width=params["width"], fix_sentence_endings=True, initial_indent=indent, subsequent_indent=indent)
        output = []
        for txt, formatted in paragraphs:
            if formatted:
                txt = wrapper.fill(txt) + "\n"
            else:
                # unformatted output, prepend every line with the indent but otherwise leave them alone
                txt = indent + ("\n" + indent).join(txt.splitlines()) + "\n"
            assert txt.endswith("\n")
            output.append(txt)
        return self.smartquotes("".join(output))

    def output(self, *lines):
        """Write some text to the screen. Takes care of style tags that are embedded."""
        for line in lines:
            print(self._apply_style(line, self.do_styles))
        sys.stdout.flush()

    def output_no_newline(self, text):
        """Like output, but just writes a single line, without end-of-line."""
        print(self._apply_style(text, self.do_styles), end="")
        sys.stdout.flush()

    def write_input_prompt(self):
        """write the input prompt '>>'"""
        print(self._apply_style("\n<dim>>></> ", self.do_styles), end="")
        sys.stdout.flush()

    def break_pressed(self):
        """do something when the player types ctrl-C (break)"""
        if threading.current_thread().name != "MainThread":
            # ony trigger the ^C handling if we're running in the main thread,
            # otherwise we could get two triggers (one from the async i/o thread, and
            # one from the main thread)
            return
        if self.stop_main_loop:
            # don't write the feedback if the loop is already stopping
            return
        print(self._apply_style("\n* break: Use <quit> if you want to quit.", self.do_styles))
        sys.stdout.flush()

    def _apply_style(self, line, do_styles):
        """Convert style tags to ansi escape sequences suitable for console text output"""
        if "<" not in line:
            return line
        elif style_colors and do_styles:
            for tag in style_colors:
                line = line.replace("<%s>" % tag, style_colors[tag])
            return line
        else:
            return iobase.strip_text_styles(line)
