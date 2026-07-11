import json
import zmq
from typing import Dict, Any, Optional

class ZmqClient:
    """
    Nitpicker Client (v28.1 Production-Ready)
    - Context Manager(with문) 지원으로 리소스(Context, Socket) 누수 원천 차단
    - 입력 페이로드 오염 방지 (얕은 복사)
    """
    # 글로벌 공유 컨텍스트 (필요시 사용)
    _shared_ctx: Optional[zmq.Context] = None

    def __init__(self, server_address: str = "tcp://127.0.0.1:5555", timeout_ms: int = 2000, use_shared_ctx: bool = True):
        self.server_address = server_address
        self.timeout_ms = timeout_ms
        self.socket: Optional[zmq.Socket] = None
        
        if use_shared_ctx:
            if ZmqClient._shared_ctx is None:
                ZmqClient._shared_ctx = zmq.Context()
            self.ctx = ZmqClient._shared_ctx
        else:
            self.ctx = zmq.Context()
            
        self._owns_ctx = not use_shared_ctx

    def __enter__(self):
        """Context Manager 지원 (소켓 자동 정리)"""
        self.socket = self.ctx.socket(zmq.REQ)
        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.connect(self.server_address)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.socket:
            self.socket.close()
            self.socket = None
        if self._owns_ctx:
            self.ctx.term()

    @classmethod
    def close_shared_context(cls) -> None:
        if cls._shared_ctx is not None:
            cls._shared_ctx.term()
            cls._shared_ctx = None

    def send(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not self.socket:
            raise RuntimeError("ZmqClient는 'with' 컨텍스트 매니저 안에서 사용해야 합니다.")

        try:
            # 방어: 호출자의 원본 딕셔너리 오염 방지
            safe_message = message.copy()
            if "schema_version" not in safe_message:
                safe_message["schema_version"] = "1.0"
                
            payload_bytes = json.dumps(safe_message, ensure_ascii=False).encode('utf-8')
            self.socket.send(payload_bytes)

            reply_bytes = self.socket.recv()
            return json.loads(reply_bytes.decode('utf-8'))

        except zmq.error.Again:
            # request_id가 있으면 에러 메시지에 포함시켜 추적성 확보
            req_id = message.get('request_id', 'UNKNOWN')
            raise TimeoutError(f"[req_id: {req_id}] Nitpicker 데몬({self.server_address}) 응답 타임아웃.")
        except zmq.ZMQError as error:
            req_id = message.get('request_id', 'UNKNOWN')
            raise ConnectionError(f"[req_id: {req_id}] ZMQ 통신 오류: {error}") from error

# --- [사용 예시] ---
# with ZmqClient() as client:
#     response = client.send({"message_type": "review.request", "request_id": "req_123", ...})