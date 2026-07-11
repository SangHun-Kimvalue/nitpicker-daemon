import json
import logging
import asyncio
import zmq
import zmq.asyncio
from typing import Callable, Awaitable, Dict, Any, Set

class ZmqRouter:
    """
    Nitpicker Daemon 통신 허브 (v28.1 Production-Ready)
    - Backpressure: Semaphore 기반 동시성 제어
    - Graceful Shutdown: In-flight 태스크 추적 및 안전한 종료
    """
    def __init__(
        self,
        bind_address: str = "tcp://127.0.0.1:5555",
        max_concurrent_jobs: int = 10,
        poll_timeout_ms: int = 250,
    ):
        self.bind_address = bind_address
        self.max_concurrent_jobs = max_concurrent_jobs
        self.poll_timeout_ms = poll_timeout_ms
        self.ctx = zmq.asyncio.Context()
        self.socket = self.ctx.socket(zmq.ROUTER)
        self.socket.setsockopt(zmq.LINGER, 0)
        self._poller = zmq.asyncio.Poller()
        self._poller.register(self.socket, zmq.POLLIN)
        
        # 동시 처리량 제한 (Resource Manager와 연동 가능)
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
        self._is_running = False
        self._shutdown_started = False
        
        # 실행 중인 태스크 추적용 Set (Graceful Shutdown 용도)
        self._background_tasks: Set[asyncio.Task] = set()

    def register_handler(self, message_type: str, handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> None:
        self._handlers[message_type] = handler

    async def start(self) -> None:
        self.socket.bind(self.bind_address)
        self._is_running = True
        logging.info(
            f"[ZmqRouter] 🚀 Nitpicker IPC 가동 (Max Concurrency: {self.max_concurrent_jobs}): {self.bind_address}"
        )

        try:
            while self._is_running:
                events = dict(await self._poller.poll(timeout=self.poll_timeout_ms))
                if self.socket not in events:
                    continue

                frames = await self.socket.recv_multipart()
                if len(frames) != 3:
                    logging.warning(f"[ZmqRouter] Invalid frames received: {len(frames)}")
                    continue

                client_id, empty, payload_bytes = frames
                if empty != b"":
                    logging.warning("[ZmqRouter] Invalid ROUTER frame delimiter received")
                    continue
                
                # Semaphore 획득 대기 (Backpressure 적용)
                await self._semaphore.acquire()
                
                # 태스크 생성 및 추적 Set에 등록
                task = asyncio.create_task(self._process_and_reply_wrapper(client_id, payload_bytes))
                self._background_tasks.add(task)
                # 태스크 완료 시 콜백으로 자원 해제(Semaphore 반납 및 추적 Set에서 제거)
                task.add_done_callback(self._on_task_done)

        except asyncio.CancelledError:
            logging.info("[ZmqRouter] 종료 시그널 수신. 새로운 요청 수락을 중단합니다.")
        finally:
            await self._graceful_shutdown()

    async def _process_and_reply_wrapper(self, client_id: bytes, payload_bytes: bytes) -> None:
        """실제 처리 로직을 감싸서 에러 추적성을 보장하는 래퍼"""
        request_id = "UNKNOWN"
        msg_type = "UNKNOWN"
        try:
            request_data = json.loads(payload_bytes.decode('utf-8'))
            request_id = request_data.get("request_id", "UNKNOWN")
            msg_type = request_data.get("message_type", "UNKNOWN")
            
            if request_data.get("schema_version") != "1.0" or not msg_type:
                raise ValueError("지원하지 않는 스키마 버전이거나 message_type이 없습니다.")

            handler = self._handlers.get(msg_type)
            if not handler:
                raise NotImplementedError(f"'{msg_type}'에 대한 핸들러가 없습니다.")

            response_payload = await handler(request_data)
            
            reply_data = {
                "schema_version": "1.0",
                "status": "success",
                "request_id": request_id,
                "message_type": msg_type,
                "response": response_payload
            }

        except asyncio.CancelledError:
            logging.info(f"[ZmqRouter] 작업 취소됨 (req_id: {request_id})")
            raise
        except Exception as e:
            logging.error(f"[ZmqRouter] 처리 중 오류 (req_id: {request_id}): {e}")
            reply_data = {
                "schema_version": "1.0",
                "status": "error",
                "request_id": request_id,     # 에러 발생 시에도 추적 키 유지
                "message_type": msg_type,
                "error_code": type(e).__name__,
                "error_message": str(e)
            }

        reply_bytes = json.dumps(reply_data, ensure_ascii=False).encode('utf-8')
        
        # 만약 셧다운 중이라 소켓이 닫혔다면 전송 포기
        if self.socket.closed:
            return

        try:
            await self.socket.send_multipart([client_id, b"", reply_bytes])
        except (zmq.ZMQError, RuntimeError) as error:
            logging.warning(f"[ZmqRouter] 응답 전송 실패 (req_id: {request_id}): {error}")

    def _on_task_done(self, task: asyncio.Task) -> None:
        """태스크가 끝나면 락을 풀고 추적 목록에서 제거"""
        self._semaphore.release()
        self._background_tasks.discard(task)

    async def _graceful_shutdown(self) -> None:
        """진행 중인 모든 태스크가 끝날 때까지 대기(Drain) 후 소켓 닫음"""
        if self._shutdown_started:
            return

        self._shutdown_started = True
        self._is_running = False
        
        if self._background_tasks:
            logging.info(f"[ZmqRouter] 남은 작업 {len(self._background_tasks)}개 대기 중... (Graceful Drain)")
            done, pending = await asyncio.wait(self._background_tasks, timeout=5.0)
            if pending:
                logging.warning(f"[ZmqRouter] 종료 타임아웃 초과. 남은 작업 {len(pending)}개 취소합니다.")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            
        logging.info("[ZmqRouter] 모든 작업 종료. 소켓과 Context를 정리합니다.")
        self._poller.unregister(self.socket)
        self.socket.close(linger=0)
        self.ctx.term()

    def stop(self) -> None:
        """외부에서 강제 종료 시그널을 보낼 때 호출"""
        self._is_running = False