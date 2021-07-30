"""

serial_sender.py - daemon to send G-code to our Matsuura over RS-232

listens on a tcp port for commands, normally from the web server,
while sending data over an RS-232 serial tty port.

Receives json encoded commands e.g.: {"cmd": "start", "file": "1001.nc"}
Response is coded as: {"error": 0, "message": "File Started"}
Error of 0 means no error.  Error of 1, means something is wrong.

Other commands supported are "stop", and "status".  Neither take an argument.
"stop" aborts the current sending file, and "status" returns a text
description of the daemon status (sending, idle, finished send, etc).

Supports simultaneous connections from the network for command and control
but only supports sending data on one RS-232 port.

Notice: This is custom configured to work with the Nova Labs Matsuura with all
it's special needs and requirements, based on how we have the machine
configured. Do not expect it to work correctly for other CNC machines without
careful testing. In addition, the Matsuura MX3 controller has many parameters
that can be adjusted for how it deals with the RS-232 port.  Such as parity,
which it set to ignore now, and the use of RTS/CTS for flow control vs
XON/XOFF (it can support both). And that it uses % to signal the end of
the G-code, not the G-code stop commands, like M30.  This code, is tuned
to work with our Matsuura as configured. Change the Matsuura configuration
and this won't likely work.

To test:

Hook 2 usb serial dongles  up to two ports
connect them with a null modem cable
run this code on one of them
run this on the other one

python3 /usr/lib/python3/dist-packages/serial/tools/miniterm.py --rtscts --rts 1 /dev/ttyUSB1 9600
# 9/29 haven't proven RTS/CTS handshake working in above config

See also: serial_receiver.py in this code base.  It is an RS-232 receiver
to replace the miniterm in the above testing procedure and emulates the
behavior of the Matsuura for stopping and starting flow with RTS to prove
that flow control is working and to help diagnose a bug we had with
RS-232 Overflow alarms on the Matsuura.

2021-07-30 This code had a major overall - Curt Welch curt@kcwc.com

Most the code changes dealt with clean handling of errors, which were
not tested for in the old version, like starting a new file, while in
the middle of sending a file, was not prevented in the old.

The RS-232 Overrun alarm bug was fixed by sending not just \n, but both
\r and \n for each line.  The bug was really on the Matsuura side in that
it did not tell the sender to stop sending fast enough, if the data being
sent were very short G-Code blocks like "M06" and "M30".  A string of these
one command blocks at the end of the job triggered the alarm.  I believe
the issue was that the the matsuura didn't expect it to be possible to
encode more than two blocks of G-code lines in less than 10 characters. With
only an on each line, these commands only take 4 characters each, and 10
characters will code more than two blocks.  This is only an educated guess
because the truth is hidden inside the Matsuura.

In addition to sending CR LF to make the commands longer, I also pad any
short lines (like M3) with spaces to make it at least three characters long.

Other changes to the code included reading the entire file into memory and
modifying it, to strip leading blank lines and an optional % start of G-code
symbol, strip all blank lines, strip trailing spaces and CR and LF then
adding spaces to short lines and CR LF to all lines.

The End of code % marker is sent without a CR LF (the Matsuura seems not
to need it, and those can end up being buffered and read at the start of
the next transfer creating confusion for the users).

Code was structured to deal with an odd Matsuura issue when drip feeding.
At the end of job, when the Matsuura halts, on an M30 (or other commands)
it stops reading from the RS-232 and turns RTS off.  If it has not read
the % before this, the % will never be read, and the send file operation
will just hand forever waiting for the Matsuura to read it.  This is mostly
harmless, the user just needs to abort the send with the stop feature. But
it creates the impression that this upload system is flaky, so I worked
to prevent that by adding the % to the end of the last line, instead of sending
it separately, so the last line of the G-code and the % get sent in one
write command to the serial port to increase the odds that the Matsuura will
read the % before it executes teh M30 and stops reading.

A main feature of this new code is that it attempts to prevent the large
OS and USB buffers from filling with G-Code not actually sent on the RS-232
line. Not doing this, creates user confusion when the application say the
file has finished sending, but yet 15K of G-code is still buffered and
being sent. For small files, it can even make the user think the file was
not sent, and cause them to hit the send button again, buffering up 2 copies
of the same code in the OS buffers.  And because the application has no way
to clear these buffers (well, mayne a tty close and re-open would do it?),
the solution I took was to just make sure the OS buffers were never filled
up.

The solution to keeping the OS buffers empty was to never send data faster
than 960 characters per second (after every write, the code will not try
to write again until enough time has passed to allow all those
to be transmitted), and second, to never send if the Matsuura had CTS
turned off, or if the local buffers had any characters backlogged. This
approach works well, but it not perfect.  Because we can't see the size
of the OS buffer, we don't know for sure that we haven't filled it up by
sending too much when the Matsuura was not ready for it.  The highly complex
issue at play, is that when we see the Matsuura ask for data, with a
CTS signal, and we write a big block of data, we might have written too much.
The Matsuura might only hav been willing to accept say 10 chars before it
turned RTS off, but we just sent 50.  If we keep making that mistake, we
OS buffers will fill up and back log.

Because the RTS.CTS works, this is not a hard error. It is ONLY a user
confusion error.  If the user aborts the run (their job is not working),
all the data in the OS buffers will keep being sent. Then when they try
to restart the next job, the old un-sent data is going to be sent to the
Matsuura.  To clear the buffers, the user must go to Memory Edit Mode,
and hit IN and RESET multiple times, to make the matsuura eat the
garbage that has been buffered.  Not a very user friendly result.  Which
is why I worked so hard with this to try and keep the OS buffers empty.

This errors on the side of slowing down the transfer, to gain the advantage
of simpler use, and less confusion, and greater reliability and trust in the
system.

Curt Welch

"""

import socket
import select
import os
import sys
from typing import Optional, List

import serial
import serial.tools.list_ports
import time
import random
import dotenv
import json

DEFAULT_SERIAL_PORT_NAME = "/dev/ttyUSB0"
DEFAULT_TCP_PORT = 1111
DEFAULT_UPLOAD_PATH = "/home/pi/matsuura_uploader/uploads"

BAUD = 9600     # Not meant to be changed

ADD_PERCENT_TO_END_OF_LAST_COMMAND = True

# TODO clean up documentation
# TODO reread all the code to look for something I missed in huge refactor
# TODO what about the last % (do we need to do special testing?)
# TODO figure out why the white cable worked when the FTDI did not.
# TODO exclusive open of tty
# TODO catch port in use bind error

DEBUG_SEND = False   # log sent data
DEBUG_FLOW = False   # log CTS changes


class SerialSender:
    """ Matsuura SerialSender Daemon
    """
    def __init__(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.read_list = []

        dotenv.load_dotenv()  # get environ vars from .env

        self.serial_port_name = \
            os.environ.get('SERIAL_PORT_NAME', DEFAULT_SERIAL_PORT_NAME)
        self.tcp_port = int(os.environ.get('SERIAL_TCP_PORT', DEFAULT_TCP_PORT))
        self.upload_path = os.environ.get('UPLOAD_PATH', DEFAULT_UPLOAD_PATH)

        self.serial_port = SerialPort(self.serial_port_name)
        self.file_to_send: Optional[FileToSend] = None

        self.sticky_status: Optional[str] = None

        self.last_cts = False
        self.time_to_check_again = time.time()

    def run(self):
        """ Main loop, never ends.
        """
        self.prep_socket()
        # sys.stderr.write(gen_send_random_string() + '\n')
        # list_ports()
        while True:
            self.serial_port.check_open()

            if self.serial_port.is_not_open and self.file_to_send is not None:
                # We lost the serial port, abort the file send.
                log(f"Lost serial port, abort sending {self.file_to_send.name}")
                self.file_to_send: Optional[FileToSend] = None

            if self.serial_port.is_open and time.time() > self.time_to_check_again:
                self.serial_chores()

            sock, mesg_from_socket = self.process_inbound_socket_connections()

            if mesg_from_socket != '':
                # process inbound message
                log(f'Message received: {mesg_from_socket!r}\n')
                mesg = json.loads(mesg_from_socket.lower())
                command = mesg.get("cmd")

                if command is None:
                    self.send_err(sock, "Missing 'cmd' label in request")

                elif command == 'start':
                    file = mesg.get("file")
                    if file is None:
                        self.send_err(sock, "Missing 'file' label in start request.")
                    else:
                        self.sticky_status: Optional[str] = None
                        self.serial_start_send(sock, file)

                elif command == 'stop':
                    if self.file_to_send is not None:
                        file_name = self.file_to_send.name
                        log(f"Closing file: {file_name}")
                        self.file_to_send: Optional[FileToSend] = None
                        self.sticky_status = f"Stopped sending: {file_name}"
                        self.send_ok(sock, self.sticky_status)
                    else:
                        self.sticky_status: Optional[str] = None
                        self.send_err(sock, "Already stopped")

                elif command == 'status':
                    m = 'Idle'
                    if self.sticky_status:
                        # This is a saved status that needs to hang around
                        # to be sure the user sees it on the next web page
                        # update.  Really useful for "file sent" but also used
                        # to make other messages sticky.
                        m = self.sticky_status

                    if self.serial_port.is_not_open:
                        m = f"Cannot open serial port: {self.serial_port.port_name}"
                    elif self.file_to_send is not None:
                        m = self.file_to_send.status

                    self.send_ok(sock, m)
                else:
                    self.send_err(sock, "Unknown command")

    def send_ok(self, sock, message):
        self.send_response(sock, 0, message)

    def send_err(self, sock, message):
        self.send_response(sock, 1, message)

    @staticmethod
    def send_response(sock, error, message):
        response = json.dumps({"error": error, "message": message})
        log(f"Response to client: {response!r}")
        sock.send(response.encode("utf-8"))

    def prep_socket(self):
        """ called once to prepare the primary tcp listener socket """
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # log(f"port is {self.tcp_port}")
        self.server_socket.bind(('', self.tcp_port))
        self.server_socket.listen(1)
        log("Listening on TCP port %d\n" % self.tcp_port)

        self.read_list = [self.server_socket]  # read list is the list of tcp ports

    def process_inbound_socket_connections(self):
        """ select() returns all the connections and their statuses """

        timeout = 1.0  # check status of serial every second
        now = time.time()
        if self.serial_port.is_open and self.file_to_send is not None:
            if self.time_to_check_again > now:
                # Sleep until it's time to check again
                timeout = self.time_to_check_again - now
            else:
                timeout = 0.02
        if timeout > 1.0:
            timeout = 1.0

        # log(f"select with timeout of {timeout:.6f} now:{now:.3f} check_again:{self.time_to_check_again:.2f}")
        readable, writable, errored = \
            select.select(self.read_list, [], [], timeout)

        for s in readable:
            # for anything inbound...
            if s is self.server_socket:
                # new connections will appear on server_socket
                client_socket, address = self.server_socket.accept()
                self.read_list.append(client_socket)    # put it on our read_list
                log("Connection from: %s:%s\n" % (address[0], address[1]))
            else:
                # handle messages from client connections
                data_buf = b''
                try:
                    data_buf = s.recv(1024)
                except OSError:
                    log('socket reset')

                if data_buf:
                    # extract message.
                    try:
                        mesg = data_buf.decode('utf-8')  # attempt to convert
                    except UnicodeError:
                        return [s, data_buf]       # send raw if unable
                    return [s, mesg.rstrip()]   # otherwise send utf-8 version
                else:
                    # otherwise connection must have shut down
                    # log("Disconnecting from client")
                    self.read_list.remove(s)
                    s.close()

        return ['', '']  # if select() returns w/nothing readable return empty

    def serial_start_send(self, sock, filename):
        """ open file and start sending on serial port """

        if self.file_to_send is not None:
            self.send_err(sock, f"Already Busy Sending {self.file_to_send.name}")
            return

        if self.serial_port.is_not_open:
            self.send_err(sock, f"Can't send, serial port is broken")
            return

        file_with_path = os.path.join(self.upload_path, filename)
        try:
            self.file_to_send = FileToSend(file_with_path)
        except OSError:
            self.file_to_send: Optional[serial.Serial] = None
            self.send_err(sock, f"open [{filename}] FAIL")
            return

        self.send_ok(sock, f"Started sending [{self.file_to_send.name}] ")

    def serial_chores(self):
        """
            call periodically
            if file is open, send another line
        """

        try:
            cts = self.serial_port.cts
        except OSError as err:
            log(f"USB Unplugged: {err.strerror}")
            # Someone unplugged the USB cable
            self.serial_port.close()
            return

        if DEBUG_FLOW:
            msg = f"FLOW: cts: {cts!s:<5}"

            if self.file_to_send is not None:
                msg += f" out_waiting: {self.serial_port.out_waiting:<3} "
                msg += f" {self.file_to_send.status}"

            if cts != self.last_cts:
                self.last_cts = cts
                log(msg)

        if self.file_to_send is None:
            return

        # if serial_connection.out_waiting == 0:
        if self.serial_port.out_waiting == 0 and self.serial_port.cts:
            line_from_file = self.file_to_send.read_line(max_size=100)
            if line_from_file is None:
                if DEBUG_SEND:
                    log(f"SEND: EOF: {self.file_to_send.status}")
                self.sticky_status = self.file_to_send.status
                self.file_to_send: Optional[FileToSend] = None
                return

            line_from_file_as_bytes = line_from_file.encode('utf-8')
            if DEBUG_SEND:
                log(f"SEND: {len(line_from_file_as_bytes):3} {line_from_file!r} ")
            bytes_sent = self.serial_port.write(line_from_file_as_bytes)
            if bytes_sent > 0:
                # Don't try to send more until these bytes have had time
                # to be sent. (9600 baud is 960 characters per second)
                # 1 stop bit, 8 data, 1 stop so 10 bits per character sent.
                self.time_to_check_again = time.time() + (bytes_sent - 1) / (BAUD/10)

            # log(f"    chore done cts: {cts!s:<5}"
            #     f" out_waiting: {self.serial_port.out_waiting:<3} "
            #     f" {self.file_to_send.status}"
            #     )


class FileToSend:
    """" File To Send to Matsuura.

        Reads entire file into memory.
        Fixes issues to prep for sending.
        Strips training spaces and \r and \n.
        Ignores/removes blank lines.

        Adds trailing spaces to ensure all lines are at least 3
            characters (not counting CR LF) to fix a timing bug with the
            Matsuura to prevent RS-232 Overrun Alarms.

        Strips % at beginning of file (common G-code convention to
            put % at the beginning and end of code), but we must not
            send it because the Matsuura stops reading on %.
            Only works if there are only blank lines before the %.  If
            there are Leader comments, this is not dealt with.

        Looks for % end marker and ignores rest of file.
        Adds % in buffer to be sent.
    """
    def __init__(self, file_name):
        """ Reads and cleans up entire file into memory on creation.
            Raises OSError on file open error. """

        self.file_name = file_name      # Full name with path
        self.line_buf: List[str] = []   # Lines of file with \n stripped off.
        self.lines_sent = 0             # Index of next line to send
        self.read_buffer = ""           # Chars waiting to be sent

        self._read_file()

    @property
    def name(self):
        """ Base file name without path. """
        return os.path.basename(self.file_name)

    @property
    def lines(self) -> int:
        """ Total number of lines from file to be sent. """
        return len(self.line_buf)

    @property
    def percent_sent(self) -> int:
        """ Percent line_buf sent (0 to 100) """
        return int(self.lines_sent * 100 / self.lines)

    @property
    def eof(self) -> bool:
        return self.lines_sent >= len(self.line_buf) and self.read_buffer == ""

    @property
    def status(self):
        """ e.g. "Sending 1001.nc, Line 89/234 38%" """
        status = f"Sending {self.name}, Line {self.lines_sent}/{self.lines} " \
                 f"{self.percent_sent}%"
        if self.lines_sent >= self.lines:
            status = f"Finished sending: {self.name}, {self.lines} lines, 100%"
        return status

    def _read_file(self) -> None:
        """ Read file into memory.

            Builds the list of line_buf to transmit (without \r or \n) and makes
            sure it starts with a blank line and ends with the needed % marker.

            Only reads up to the % End-of-code marker and skips a beginning
            % if there is one.

            open() will throw OSError exception
        """

        self.line_buf = []

        saw_start_percent = False
        with open(self.file_name) as fd:
            while True:
                line = fd.readline()
                if line == "":  # EOF
                    break
                line = line.rstrip().upper()    # Strip \n, spaces, make upper
                if len(self.line_buf) == 0:
                    if line == "":
                        # skip all initial blank line_buf.
                        continue
                    if not saw_start_percent and line[0] == "%":
                        # We treat an initial '%' as a G code start of code
                        # marker but we can not send it because the Matsuura
                        # will treat it as an end of code marker and stop
                        # reading. So we strip it, but we only strip one. The
                        # next one we see is the end of code marker.
                        saw_start_percent = True
                        continue
                if line == "":
                    # Strip all blank lines.
                    continue
                # We have a non blank line
                if line[0] == "%":  # end of code marker
                    break
                while len(line) < 3:
                    # Short lines like "M06\n" (4 chars) seemed to have been
                    # a key part of the Matsuura RS-232 Over-run Alarm so
                    # I'm going to just pad all short lines with spaces
                    # to make sure "M6" becomes "M6 " as well
                    # as adding \r\n instead of just \n.
                    line += ' '
                self.line_buf.append(line)

        # End of file.

        # Add a % to the end of the line buffer.
        #
        # Because there is a really odd bug here we add it to the end of the
        # last line so it gets sent at the same time the last line is sent. The
        # bug is if we are drip feeding slowly, and the M30 stop command at the
        # end of the file gets executed before we send the %, then the Matsuura
        # stops reading.  So CTS will never go low and we will be hung waiting
        # for the Matsuura to ask for another line.  In drip feed (TAPE) mode,
        # we could imply never bother to send the %. But when sending to load a
        # program into memory, the % is required.  Since we don't know if we
        # are drip feeding or loading into memory, we must send the %.  So the
        # simple hack I choose to use here, is to send it as part of the last
        # line of the file.

        if ADD_PERCENT_TO_END_OF_LAST_COMMAND:
            # Use this complex hack
            if len(self.line_buf) == 0:
                # There is no last line to add it to!
                self.line_buf.append("%")
            else:
                self.line_buf[-1] += "\r\n%"
        else:
            # Don't use the hack, just put the % on a line by itself.
            self.line_buf.append("%")

        # Add initial blank line for the Matsuura LSK (Leader Skip) to eat.
        self.line_buf.insert(0, "")

        self.lines_sent = 0

    def read_line(self, max_size=0) -> Optional[str]:
        """ Return next line to send (with CR LF added)
            Returns None for EOF.
            max_size is the size limit of the returned data.
            max_size == 0 means no limit.
        """
        if self.eof:
            return None
        if self.read_buffer:
            line = self.read_buffer
        else:
            line = self.line_buf[self.lines_sent]
            self.lines_sent += 1
            if not (len(line) and line[-1] == '%'):
                # Add \r\n to line unless it's the % end-of-file-marker.
                # We don't add it after the % because the Matsuura won't read
                # the \n then it gets left in the buffers to be read at the
                # beginning of the next input operation which can cause issues.
                #
                # *** MUST *** add CR LF and not just LF because short lines
                # triggered the RS-232 Overrun Alarm bug.
                # Changing LF to CR LF fixed it.
                line += "\r\n"
        if max_size:
            # Split into two parts
            self.read_buffer = line[max_size:]
            line = line[:max_size]
        else:
            self.read_buffer = ""
        return line


class SerialPort:
    """ The serial port to talk to the Matsuura. """
    def __init__(self, port_name: str):
        self.port_name = port_name      # e.g. "/dev/ttyUSB0"
        self.serial_connection: Optional[serial.Serial] = None
        self.check_open()

    def check_open(self):
        """ Check if port is open and working. Try to open if not.

            return False if not, True if open and working.

            self.err is exception if check failed.
        """
        # log(f"check open {self.is_open}")
        if self.is_open:
            # To verify it's still connected (USB not unplugged), check cts
            try:
                _ = self.cts
            except OSError:
                log(f"Lost Connection to {self.port_name}")
                # will try to open below
                self.serial_connection: Optional[serial.Serial] = None

        if self.is_not_open:
            # if serial connection is not open attempt to open
            try:
                self.open()
            except serial.SerialException:
                log(f"Cannot open: {self.port_name}")
                self.serial_connection: Optional[serial.Serial] = None
                return False

            log(f"Serial port open: {self.port_name}")

        return True

    def open(self):
        """ Open port with the correct Matsuura parameters.
            9600 baud, 8 bit, No Parity, RTS/CTS Hardware Handshaking.
            Will raise serial.SerialException on error.
        """
        self.serial_connection = serial.Serial(self.port_name,
                                               9600,
                                               parity=serial.PARITY_NONE,
                                               write_timeout=None,
                                               xonxoff=False,
                                               rtscts=True)

    @property
    def is_open(self) -> bool:
        return not self.is_not_open

    @property
    def is_not_open(self) -> bool:
        return self.serial_connection is None

    @property
    def cts(self) -> bool:
        """ CTS wire is True or False. """
        if self.is_open:
            return self.serial_connection.cts
        return False

    @property
    def rts(self):
        if self.is_open:
            return self.serial_connection.rts
        return False

    @rts.setter
    def rts(self, value: bool):
        if self.is_open:
            self.serial_connection.rts = value

    def read_all(self):
        """ Read bytes from serial port. Returns what is available.

            Might return None at times, I'm not sure...
        """
        if self.is_open:
            return self.serial_connection.read_all()
        return ""

    def write(self, byte_buf):
        """ Write bytes to serial port. Will block if you write too many. """
        if self.is_open:
            return self.serial_connection.write(byte_buf)
        return None

    def close(self):
        if self.is_open:
            self.serial_connection.close()
        self.serial_connection: Optional[serial.Serial] = None

    @property
    def out_waiting(self) -> int:
        """ Number of chars buffered in local output buffer.
            There are other buffers for USB ports that do not show up
            in this number.  Testing on a MacBook, the write to the
            port would hang when this number plus the characters to write
            exceed about 512.
        """
        if self.is_open:
            return self.serial_connection.out_waiting
        return 0


def log(s: str):
    """ Write string s to stderr with ms timestamp. """
    now = time.time()
    m_sec = int(now*1000 % 1000)
    message = s
    if message[-1] == '\n':
        message = message[:-1]
    t = time.localtime(now)
    sys.stderr.write(f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}.{m_sec:03d}")
    sys.stderr.write(f" {message}\n")


def list_ports():
    # list available ports. For debugging
    iterator = serial.tools.list_ports.comports()
    port_names = []
    for n, (port, desc, hwid) in enumerate(iterator, 1):
        port_names.append(port)
        log("%d %s\n" % (n, port))
        log("    desc: {}\n".format(desc))
        log("    hwid: {}\n".format(hwid))
    return port_names


def gen_send_random_string():
    # you never know when you are going to need to send a random string..
    # noinspection SpellCheckingInspection
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return ''.join([random.choice(chars) for _ in range(32)])


if __name__ == '__main__':
    SerialSender().run()
    exit(0)
