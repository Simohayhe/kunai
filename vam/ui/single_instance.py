"""Kunai を二重に起動させない。

トレイ常駐 (閉じてもプロセスが残る) にしてから、アイコンをダブル
クリックし直すなどで気づかないうちに2つ目が立ち上がることがある。
QLocalServer/QLocalSocket で「もう1つ動いているか」を確かめ、動いて
いれば新しい方はウィンドウを前面に出すよう頼んでからすぐ終了する。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "KunaiSingleInstance"


class SingleInstanceGuard(QObject):
    show_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server: QLocalServer | None = None

    def try_lock(self) -> bool:
        """既に1つ動いていれば、そちらへ知らせて False を返す
        (呼び出し元はここで起動をやめる)。動いていなければ、次のために
        待ち受けを始めて True を返す。
        """
        socket = QLocalSocket()
        socket.connectToServer(SERVER_NAME)
        if socket.waitForConnected(200):
            socket.write(b"show")
            socket.waitForBytesWritten(200)
            socket.disconnectFromServer()
            return False

        # 繋がらなかった。前回が異常終了してソケットの残骸が残っている
        # ことがあるので、待ち受け直前に一度消しておく。
        QLocalServer.removeServer(SERVER_NAME)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.listen(SERVER_NAME)
        return True

    def _on_new_connection(self) -> None:
        while self._server and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        socket.readAll()
        self.show_requested.emit()
        socket.disconnectFromServer()
