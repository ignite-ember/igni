"""HTTP server."""
import http.server

def serve():
    server = http.server.HTTPServer
    addr = ("0.0.0.0", 8080)
    return server
