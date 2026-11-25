import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная панель Яндекс Директа")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "direct_module.web:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
