"""Cliente minimo do protocolo MySQL, compativel com servidores 3.23 / 4.0 / 4.1+.

Os drivers atuais (PyMySQL, mysql-connector) exigem o protocolo 4.1 e nao
implementam mais a autenticacao "old password" (hash de 16 caracteres), por isso
este modulo fala o protocolo diretamente via socket, sem dependencias externas.
"""
import hashlib
import socket
import struct
import time

# Capability flags
CLIENT_LONG_PASSWORD = 0x0001
CLIENT_FOUND_ROWS = 0x0002
CLIENT_LONG_FLAG = 0x0004
CLIENT_CONNECT_WITH_DB = 0x0008
CLIENT_PROTOCOL_41 = 0x0200
CLIENT_TRANSACTIONS = 0x2000
CLIENT_SECURE_CONNECTION = 0x8000

COM_QUIT = 0x01
COM_INIT_DB = 0x02
COM_QUERY = 0x03
COM_PING = 0x0E

MAX_PACKET = 0xFFFFFF

# Tipos de coluna numericos (para exportacao)
NUMERIC_TYPES = {0, 1, 2, 3, 4, 5, 8, 9, 13, 246}

# Charsets do MySQL -> (id do protocolo 4.1, codec Python)
CHARSETS = {
    "latin1": (8, "cp1252"),   # o "latin1" do MySQL e na verdade cp1252
    "utf8": (33, "utf-8"),
    "latin2": (9, "iso-8859-2"),
    "ascii": (11, "ascii"),
}


class MySQLError(Exception):
    def __init__(self, errno, message, sqlstate=None):
        self.errno = errno
        self.sqlstate = sqlstate
        self.message = message
        text = f"#{errno}"
        if sqlstate:
            text += f" ({sqlstate})"
        super().__init__(f"{text} - {message}")


class Column:
    __slots__ = ("name", "table", "type", "length")

    def __init__(self, name, table, type_, length):
        self.name = name
        self.table = table
        self.type = type_
        self.length = length

    @property
    def is_numeric(self):
        return self.type in NUMERIC_TYPES


class Result:
    """Resultado de uma instrucao: ou um conjunto de linhas ou um OK."""

    def __init__(self, sql):
        self.sql = sql
        self.columns = []
        self.rows = []
        self.affected_rows = 0
        self.insert_id = 0
        self.message = ""
        self.elapsed = 0.0

    @property
    def has_rows(self):
        return bool(self.columns)


# ---------------------------------------------------------------- autenticacao

def _hash_password_323(password: bytes):
    nr, add, nr2 = 1345345333, 7, 0x12345671
    for c in password:
        if c in (32, 9):
            continue
        nr ^= (((nr & 63) + add) * c) + ((nr << 8) & 0xFFFFFFFF)
        nr &= 0xFFFFFFFF
        nr2 = (nr2 + (((nr2 << 8) & 0xFFFFFFFF) ^ nr)) & 0xFFFFFFFF
        add = (add + c) & 0xFFFFFFFF
    return nr & 0x7FFFFFFF, nr2 & 0x7FFFFFFF


def scramble_323(message: bytes, password: bytes) -> bytes:
    """Scramble da autenticacao antiga (MySQL 3.23 / 4.0 / OLD_PASSWORD)."""
    if not password:
        return b""
    hp = _hash_password_323(password)
    hm = _hash_password_323(message[:8])
    max_value = 0x3FFFFFFF
    seed1 = (hp[0] ^ hm[0]) % max_value
    seed2 = (hp[1] ^ hm[1]) % max_value

    def rnd():
        nonlocal seed1, seed2
        seed1 = (seed1 * 3 + seed2) % max_value
        seed2 = (seed1 + seed2 + 33) % max_value
        return seed1 / max_value

    out = [int(rnd() * 31) + 64 for _ in range(len(message[:8]))]
    extra = int(rnd() * 31)
    return bytes(c ^ extra for c in out)


def scramble_native(scramble: bytes, password: bytes) -> bytes:
    """Scramble da autenticacao 4.1+ (mysql_native_password)."""
    if not password:
        return b""
    stage1 = hashlib.sha1(password).digest()
    stage2 = hashlib.sha1(stage1).digest()
    stage3 = hashlib.sha1(scramble[:20] + stage2).digest()
    return bytes(a ^ b for a, b in zip(stage1, stage3))


# ---------------------------------------------------------------- leitura

class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def eof(self):
        return self.pos >= len(self.data)

    def read(self, n):
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def uint(self, n):
        v = int.from_bytes(self.data[self.pos:self.pos + n], "little")
        self.pos += n
        return v

    def lenenc_int(self):
        first = self.u8()
        if first < 251:
            return first
        if first == 251:
            return None
        if first == 252:
            return self.uint(2)
        if first == 253:
            return self.uint(3)
        return self.uint(8)

    def lenenc_bytes(self):
        n = self.lenenc_int()
        if n is None:
            return None
        return self.read(n)

    def nul_bytes(self):
        end = self.data.index(b"\0", self.pos)
        v = self.data[self.pos:end]
        self.pos = end + 1
        return v

    def rest(self):
        v = self.data[self.pos:]
        self.pos = len(self.data)
        return v


def _lenenc_int_bytes(n):
    if n < 251:
        return bytes([n])
    if n < 1 << 16:
        return b"\xfc" + n.to_bytes(2, "little")
    if n < 1 << 24:
        return b"\xfd" + n.to_bytes(3, "little")
    return b"\xfe" + n.to_bytes(8, "little")


# ---------------------------------------------------------------- conexao

class Connection:
    def __init__(self, host, port=3306, user="", password="", database="",
                 charset="latin1", connect_timeout=10, read_timeout=None):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database or ""
        self.charset = charset if charset in CHARSETS else "latin1"
        self.charset_id, self.encoding = CHARSETS[self.charset]
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.sock = None
        self.seq = 0
        self.server_version = ""
        self.server_caps = 0
        self.protocol41 = False
        self._connect()

    # --- baixo nivel

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Conexao encerrada pelo servidor")
            buf += chunk
        return bytes(buf)

    def _read_packet(self):
        payload = b""
        while True:
            header = self._recv_exact(4)
            length = int.from_bytes(header[:3], "little")
            self.seq = (header[3] + 1) & 0xFF
            payload += self._recv_exact(length)
            if length < MAX_PACKET:
                return payload

    def _write_packet(self, payload: bytes):
        while True:
            chunk = payload[:MAX_PACKET]
            payload = payload[MAX_PACKET:]
            header = len(chunk).to_bytes(3, "little") + bytes([self.seq])
            self.sock.sendall(header + chunk)
            self.seq = (self.seq + 1) & 0xFF
            if len(chunk) < MAX_PACKET:
                return

    def _command(self, cmd, arg=b""):
        if self.sock is None:
            raise ConnectionError("Nao conectado")
        self.seq = 0
        self._write_packet(bytes([cmd]) + arg)

    def _raise_error(self, packet):
        r = _Reader(packet)
        r.u8()
        errno = r.uint(2)
        sqlstate = None
        if self.protocol41 and r.data[r.pos:r.pos + 1] == b"#":
            r.read(1)
            sqlstate = r.read(5).decode("ascii", "replace")
        raise MySQLError(errno, r.rest().decode(self.encoding, "replace"), sqlstate)

    @staticmethod
    def _is_eof(packet):
        return packet[:1] == b"\xfe" and len(packet) < 9

    def _parse_ok(self, packet, result):
        r = _Reader(packet)
        r.u8()
        result.affected_rows = r.lenenc_int() or 0
        result.insert_id = r.lenenc_int() or 0
        if self.protocol41:
            r.read(4)  # status + warnings
        elif self.server_caps & CLIENT_TRANSACTIONS:
            r.read(2)
        if not r.eof():
            result.message = r.rest().decode(self.encoding, "replace")

    # --- handshake

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        self.sock.settimeout(self.read_timeout)
        try:
            self._handshake()
        except Exception:
            self.sock.close()
            self.sock = None
            raise

    def _handshake(self):
        packet = self._read_packet()
        if packet[:1] == b"\xff":
            self._raise_error(packet)
        r = _Reader(packet)
        protocol_version = r.u8()
        if protocol_version != 10:
            raise ConnectionError(f"Versao de protocolo nao suportada: {protocol_version}")
        self.server_version = r.nul_bytes().decode("latin1")
        r.uint(4)  # thread id
        scramble = r.read(8)
        r.read(1)
        self.server_caps = r.uint(2)
        if not r.eof():
            r.u8()       # charset
            r.uint(2)    # status
            if self.server_caps & CLIENT_PROTOCOL_41:
                self.server_caps |= r.uint(2) << 16
                r.u8()        # tamanho do auth-plugin-data
                r.read(10)    # reservado
                scramble += r.read(12)
            # servidores 4.0 enviam 13 bytes de enchimento aqui; ignoramos

        self.protocol41 = bool(self.server_caps & CLIENT_PROTOCOL_41)
        secure = bool(self.server_caps & CLIENT_SECURE_CONNECTION) and len(scramble) >= 20

        user = self.user.encode(self.encoding)
        password = self.password.encode(self.encoding)
        caps = CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG | CLIENT_TRANSACTIONS
        use_db = bool(self.database) and self.server_caps & CLIENT_CONNECT_WITH_DB
        if use_db:
            caps |= CLIENT_CONNECT_WITH_DB

        if self.protocol41:
            caps |= CLIENT_PROTOCOL_41
            if secure:
                caps |= CLIENT_SECURE_CONNECTION
            caps &= self.server_caps | CLIENT_PROTOCOL_41
            payload = struct.pack("<IIB23x", caps, 16 * 1024 * 1024, self.charset_id)
            payload += user + b"\0"
            if secure:
                token = scramble_native(scramble, password)
                payload += bytes([len(token)]) + token
            else:
                payload += scramble_323(scramble, password) + b"\0"
        else:
            caps &= self.server_caps | CLIENT_LONG_PASSWORD
            payload = struct.pack("<H", caps & 0xFFFF) + (16 * 1024 * 1024 - 1).to_bytes(3, "little")
            payload += user + b"\0" + scramble_323(scramble, password) + b"\0"
        if use_db:
            payload += self.database.encode(self.encoding) + b"\0"
        self._write_packet(payload)

        packet = self._read_packet()
        if self._is_eof(packet):
            # Servidor 4.1 com conta usando hash antigo: pede o scramble 3.23
            self._write_packet(scramble_323(scramble, password) + b"\0")
            packet = self._read_packet()
        if packet[:1] == b"\xff":
            self._raise_error(packet)
        if packet[:1] != b"\x00":
            raise ConnectionError("Resposta inesperada na autenticacao")

        if self.database and not use_db:
            self.select_db(self.database)
        if self.protocol41:
            # Garante que o servidor devolva os dados no charset escolhido
            try:
                self.query(f"SET NAMES {self.charset}")
            except MySQLError:
                pass

    # --- API

    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        if self.sock is None:
            return
        try:
            self._command(COM_QUIT)
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def ping(self):
        self._command(COM_PING)
        packet = self._read_packet()
        if packet[:1] == b"\xff":
            self._raise_error(packet)

    def select_db(self, name):
        self._command(COM_INIT_DB, name.encode(self.encoding))
        packet = self._read_packet()
        if packet[:1] == b"\xff":
            self._raise_error(packet)
        self.database = name

    def query(self, sql, max_rows=None):
        """Executa uma instrucao e devolve um Result.

        max_rows limita as linhas guardadas em memoria (as demais sao lidas e
        descartadas para manter a conexao sincronizada).
        """
        started = time.perf_counter()
        result = Result(sql)
        try:
            self._command(COM_QUERY, sql.encode(self.encoding))
            packet = self._read_packet()
            first = packet[:1]
            if first == b"\xff":
                self._raise_error(packet)
            if first == b"\x00":
                self._parse_ok(packet, result)
            elif first == b"\xfb":
                raise MySQLError(0, "LOAD DATA LOCAL INFILE nao e suportado")
            else:
                self._read_resultset(packet, result, max_rows)
        except (OSError, ConnectionError):
            # Conexao quebrada: marca como desconectado para forcar reconexao
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None
            raise
        result.elapsed = time.perf_counter() - started
        return result

    def _read_resultset(self, packet, result, max_rows):
        count = _Reader(packet).lenenc_int()
        enc = self.encoding
        for _ in range(count):
            r = _Reader(self._read_packet())
            if self.protocol41:
                r.lenenc_bytes()  # catalog
                r.lenenc_bytes()  # schema
                table = r.lenenc_bytes()
                r.lenenc_bytes()  # org_table
                name = r.lenenc_bytes()
                r.lenenc_bytes()  # org_name
                r.lenenc_int()
                r.uint(2)         # charset
                length = r.uint(4)
                type_ = r.u8()
            else:
                table = r.lenenc_bytes()
                name = r.lenenc_bytes()
                length = int.from_bytes(r.lenenc_bytes() or b"", "little")
                type_ = (r.lenenc_bytes() or b"\xfd")[0]
            result.columns.append(Column(name.decode(enc, "replace"),
                                         (table or b"").decode(enc, "replace"),
                                         type_, length))
        packet = self._read_packet()
        if not self._is_eof(packet):
            raise ConnectionError("Esperado EOF apos definicao de colunas")

        rows = result.rows
        truncated = False
        while True:
            packet = self._read_packet()
            if self._is_eof(packet):
                break
            if packet[:1] == b"\xff":
                self._raise_error(packet)
            if max_rows is not None and len(rows) >= max_rows:
                truncated = True
                continue
            r = _Reader(packet)
            row = []
            for _ in range(count):
                v = r.lenenc_bytes()
                row.append(None if v is None else v.decode(enc, "replace"))
            rows.append(row)
        if truncated:
            result.message = f"Resultado limitado a {max_rows} linhas"


def escape_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def split_statements(sql: str):
    """Separa um script em instrucoes por ';', respeitando aspas e comentarios."""
    statements = []
    buf = []
    i, n = 0, len(sql)
    quote = None
    while i < n:
        c = sql[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote != "`" and i + 1 < n:
                buf.append(sql[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            buf.append(c)
        elif c == "#" or (c == "-" and sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        elif c == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue
        elif c == ";":
            stmt = "".join(buf).strip()
            if _has_code(stmt):
                statements.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    stmt = "".join(buf).strip()
    if _has_code(stmt):
        statements.append(stmt)
    return statements


def _has_code(stmt):
    """True se a instrucao tem algo alem de comentarios."""
    lines = []
    for line in stmt.splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith("-- ") or s == "--":
            continue
        lines.append(s)
    text = " ".join(lines)
    while "/*" in text:
        start = text.index("/*")
        end = text.find("*/", start)
        text = text[:start] + (text[end + 2:] if end != -1 else "")
    return bool(text.strip())
