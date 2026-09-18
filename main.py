from http.server import HTTPServer, SimpleHTTPRequestHandler

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), SimpleHTTPRequestHandler).serve_forever()
