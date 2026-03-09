FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir pytest pytest-cov pytest-asyncio

COPY docker_socket_proxy.py test_docker_socket_proxy.py ./

CMD ["python3", "-m", "pytest", "test_docker_socket_proxy.py", "-v", "--tb=short", \
     "--cov=docker_socket_proxy", "--cov-report=term-missing", "--cov-fail-under=100"]
