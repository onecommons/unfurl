import click
from flask import Flask, request, jsonify, current_app
from flask_caching import Cache
import uvicorn

from unfurl.localenv import LocalEnv


flaskConfig = {
    # Use in-memory caching, see https://flask-caching.readthedocs.io/en/latest/#built-in-cache-backends for more options
    "CACHE_TYPE": "simple", 
}
app = Flask(__name__)
app.config.from_mapping(flaskConfig)
cache = Cache(app)

@app.before_request
def hook():
    """
    Run before every request. If the secret is specified, check all requests for the secret.
    Secret can be in the secret query parameter (localhost:8080/health?secret=<secret>) or as an
    Authorization bearer token (Authorization=Bearer <secret>).
    """
    secret = current_app.config["UNFURL_SECRET"]
    if secret is None: # No secret specified, no authentication required
        return

    qs_secret = request.args.get("secret") # Get secret from query string
    header_secret = request.headers.get('Authorization') # Get secret from Authorization header
    if header_secret is not None:
        try:
            # Remove "Bearer " from header
            header_secret = header_secret.split(' ')[1]
        except IndexError: # Quick sanity check to make sure the header is formatted correctly
            return jsonify({
                "code": "BAD_REQUEST",
                "message": "The Authorization header must be in the format 'Bearer <secret>'"
            }), 400 # BAD_REQUEST

    if secret not in [qs_secret, header_secret]: # No valid secret found in headers or qs
        return jsonify({
            "code": "UNAUTHORIZED",
            "message": "Please pass the secret as a query parameter or as an Authorization bearer token"
        }), 401 # Unauthorized


@app.route("/health")
def health():
    return "OK"


@app.route("/export")
@cache.cached(query_string=True) # Ensure that the request cached includes the query string (the response differs between different formats)
def export():
    format = request.args.get("format", "deployment")
    if format not in ["blueprint", "environments", "deployment"]:
        return jsonify({
            "code": "BAD_REQUEST",
            "message": "Query parameter 'format' must be one of 'blueprint', 'environments' or 'deployment'"
        }), 400 # BAD_REQUEST

    from . import to_json
    localEnv = LocalEnv(
        current_app.config["UNFURL_ENSEMBLE_PATH"],
        current_app.config["UNFURL_OPTIONS"].get("home"),
        override_context=current_app.config["UNFURL_OPTIONS"].get("use_environment"),
    )
    exporter = getattr(to_json, "to_" + format)
    jsonSummary = exporter(localEnv)

    return jsonify(jsonSummary)


def serve(
    host: str,
    port: int,
    secret: str,
    project_or_ensemble_path: click.Path,
    options: dict,
):
    """Start a simple HTTP server which will expose part of the CLI's API.

    Args:
        host (str): Which host to bind to (0.0.0.0 will allow external connections)
        port (int): Port to listen to (defaults to 8080)
        secret (str): The secret to use to authenticate requests
        project_or_ensemble_path (click.Path): The path of the ensemble or project to base requests on
        options (dict): Additional options to pass to the server (as passed to the unfurl CLI)
    """
    app.config["UNFURL_SECRET"] = secret
    app.config["UNFURL_OPTIONS"] = options
    app.config["UNFURL_ENSEMBLE_PATH"] = project_or_ensemble_path

    # Start one WSGI server
    uvicorn.run(app, host=host, port=port, interface="wsgi", log_level="info")

    # app.run(host=host, port=port)
