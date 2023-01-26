import base64
import json
import os
import random
import string
import tempfile
from datetime import datetime
from functools import wraps
from io import StringIO
from os import environ as env
from urllib.parse import quote_plus, urlencode

import config
import pandas as pd
import requests
from authlib.integrations.flask_client import OAuth, OAuthError
from flask import Flask, redirect, render_template, request, session, url_for
from flask_session import Session
from utils.s3_helper import S3Helper
from werkzeug.exceptions import HTTPException


#####################################
# Environment loading
#####################################

HWPC_INPUT_BUCKET = env.get("S3_INPUT_BUCKET", "hwpc")
HWPC_OUTPUT_BUCKET = env.get("S3_OUTPUT_BUCKET", "hwpc-output")

_flask_debug = env.get("FLASK_DEBUG", default=True)
FLASK_DEBUG = _flask_debug.lower() in {"1", "t", "true"}

PORT = int(env.get("PORT", 8080))

FSAPPS_CLIENT_ID = env.get("FSAPPS_CLIENT_ID")
FSAPPS_CLIENT_SECRET = env.get("FSAPPS_CLIENT_SECRET")
FSAPPS_REDIRECT_URI = env.get("FSAPPS_REDIRECT_URI")

FSAPPS_API_BASE_URL = env.get("FSAPPS_API_BASE_URL")
FSAPPS_REQUEST_TOKEN_URL = FSAPPS_API_BASE_URL + env.get("FSAPPS_REQUEST_TOKEN_URL")
FSAPPS_REQUEST_TOKEN_PARAMS = env.get("FSAPPS_REQUEST_TOKEN_PARAMS")
FSAPPS_AUTHORIZE_URL = FSAPPS_API_BASE_URL + env.get("FSAPPS_AUTHORIZE_URL")
FSAPPS_AUTHORIZE_PARAMS = env.get("FSAPPS_AUTHORIZE_PARAMS")

SECRET_KEY = env.get("FLASK_SECRET_KEY")


#####################################
# Flask setup
#####################################


app = Flask(__name__, template_folder="templates")
app.secret_key = env.get("FLASK_SECRET_KEY")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

oauth = OAuth(app)

eauth = oauth.register(
    name="eauth",
    client_id=FSAPPS_CLIENT_ID,
    client_secret=FSAPPS_CLIENT_SECRET,
    access_token_url=FSAPPS_REQUEST_TOKEN_URL,
    access_token_params=None,
    authorize_url=FSAPPS_AUTHORIZE_URL,
    authorize_params=None,
    api_base_url="",
    client_kwargs={"scope": "usdaemail"},
)


#####################################
## Static application strings
#####################################

user_data_folder = "hwpc-user-inputs/"
user_data_output_folder = "hwpc-user-outputs/"
user_json_path = "/user_input.json"
calulator_html_path = "pages/calculator.html"

#####################################
## Route handlers
#####################################

# Routing for html template files


@app.route("/")
@app.route("/login", methods=["GET"])
def login():
    # This code will be created by the OAuth provider and returned AFTER a
    # successful /authorize

    authorized_code = request.args.get("code")

    state = "".join(random.choices(string.ascii_letters + string.digits, k=6))

    redirect_uri = FSAPPS_REDIRECT_URI
    # url was giving me issues with how it was formatted so I made it a string
    url = (
        FSAPPS_AUTHORIZE_URL
        + "?client_id="
        + FSAPPS_CLIENT_ID
        + "&redirect_uri="
        + redirect_uri
        + "&response_type=code&state="
        + state
    )

    if authorized_code is not None:
        print(f"Caught code {authorized_code}")
        print(f"Basic {FSAPPS_CLIENT_SECRET} =")
        url = FSAPPS_REQUEST_TOKEN_URL

        payload = {
            "grant_type": "authorization_code",
            "redirect_uri": FSAPPS_REDIRECT_URI,
            "code": f"{authorized_code}",
        }
        files = []
        headers = {
            "Authorization": "Basic " + FSAPPS_CLIENT_SECRET + "=",
            "Accept": "application/json",
        }

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files
        )
        if response.text is not None:
            url = (
                FSAPPS_API_BASE_URL
                + "me?access_token="
                + response.json()["access_token"]
            )

            payload = {}
            headers = {}

            token_response = requests.request("GET", url, headers=headers, data=payload)

            print(token_response.text)
            session["name"] = token_response.json()["usdafirstname"]
            session["email"] = token_response.json()["usdaemail"]
            email_info = token_response.json()["usdaemail"]

            print(session["email"])
        return home()

    return render_template(
        "pages/login.html", url=url, state=state, redirect_uri=redirect_uri
    )


def login_required(f):
    @wraps(f)
    def login_function(*args, **kwargs):
        if session.get("email") is None:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return login_function


@app.route("/logout")
def logout():
    # remove the email from the session if it's there
    session.pop("email", None)
    for key in list(session.keys()):
        session.pop(key)

    # key_list = list(session.keys())
    #     for key in key_list:
    #         session.pop(key)
    return redirect(url_for("login"))


@app.route("/index")
@app.route("/home", methods=["GET", "POST"])
@login_required
def home():
    return render_template("pages/home.html", email_info=session["email"])


@app.route("/calculator", methods=["GET"])
@login_required
def calculator():
    return render_template("pages/calculator.html", email_info=session["email"])


@app.route("/reference", methods=["GET"])
@login_required
def test():
    return render_template("pages/reference.html", session=session.get("user"))


@app.route("/privacy", methods=["GET"])
@login_required
def advanced():
    return render_template("pages/privacy.html", session=session.get("user"))


@app.route("/terms", methods=["GET"])
@login_required
def references():
    return render_template("pages/terms.html", session=session.get("user"))


@app.route("/contact", methods=["GET"])
@login_required
def contact():
    return render_template("contact.html", session=session.get("user"))


@app.route("/files", methods=["GET"])
def files():
    return render_template("files.html", session=session.get("user"))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    # All inputs from the UI are pulled here through with jquery Ajax
    yearly_harvest_input = request.files["yearlyharvestinput"]
    if yearly_harvest_input.filename != "":
        yearly_harvest_input = pd.read_csv(yearly_harvest_input)
        yearly_harvest_input.columns = yearly_harvest_input.columns.str.replace(" ", "")
        if len(yearly_harvest_input.columns) > 2:
            # Ensures that any long formatted data will start with YearID to begin the melt process
            yearly_harvest_input.rename(
                columns={yearly_harvest_input.columns[0]: "YearID"}, inplace=True
            )
            yearly_harvest_input = yearly_harvest_input.melt(
                id_vars="YearID", var_name="Year", value_name="ccf"
            )

            yearly_harvest_input = yearly_harvest_input[
                yearly_harvest_input["ccf"] != 0
            ]
            yearly_harvest_input = yearly_harvest_input.drop(["YearID"], axis=1)
            start_year = str(yearly_harvest_input["Year"].min())
            stop_year = str(yearly_harvest_input["Year"].max())
            for i in yearly_harvest_input.columns:
                if yearly_harvest_input[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: Harvest Data Column: " + i,
                    )

            yearly_harvest_input = yearly_harvest_input.to_csv(index=False)
        else:
            yearly_harvest_input.rename(
                columns={
                    yearly_harvest_input.columns[0]: "Year",
                    yearly_harvest_input.columns[1]: "ccf",
                },
                inplace=True,
            )
            for i in yearly_harvest_input.columns:
                if yearly_harvest_input[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: Harvest Data Column: " + i,
                    )

            start_year = str(yearly_harvest_input["Year"].min())
            stop_year = str(yearly_harvest_input["Year"].max())
            yearly_harvest_input = yearly_harvest_input.to_csv(index=False)

    harvest_data_type = request.form["harvestdatatype"]
    timber_product_ratios = request.files["yearlytimberproductratios"]

    if timber_product_ratios.filename != "":
        timber_product_ratios = pd.read_csv(timber_product_ratios)
        timber_product_ratios.columns = timber_product_ratios.columns.str.replace(
            " ", ""
        )
        if len(timber_product_ratios.columns) > 3:
            # Ensures that any long formatted data will start with TimberProductID to begin the melt process
            timber_product_ratios.rename(
                columns={timber_product_ratios.columns[0]: "TimberProductID"},
                inplace=True,
            )
            timber_product_ratios = timber_product_ratios.melt(
                id_vars="TimberProductID", var_name="Year", value_name="Ratio"
            )
            timber_product_ratios.rename(
                columns={
                    timber_product_ratios.columns[0]: "TimberProductID",
                    timber_product_ratios.columns[1]: "Year",
                    timber_product_ratios.columns[2]: "Ratio",
                },
                inplace=True,
            )
            for i in timber_product_ratios.columns:
                if timber_product_ratios[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: Timber Product Ratios Column: "
                        + i,
                    )
            timber_product_ratios = timber_product_ratios.to_csv(index=False)

        else:
            timber_product_ratios.rename(
                columns={
                    timber_product_ratios.columns[0]: "TimberProductID",
                    timber_product_ratios.columns[1]: "Year",
                    timber_product_ratios.columns[2]: "Ratio",
                },
                inplace=True,
            )
            for i in timber_product_ratios.columns:
                if timber_product_ratios[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: Timber Product Ratios Column: "
                        + i,
                    )
            timber_product_ratios = timber_product_ratios.to_csv(index=False)

    region_selection = request.form["regionselection"]
    if region_selection == "Custom":
        custom_region_file = request.files["customregion"]
        if custom_region_file.filename != "":
            custom_region_file = pd.read_csv(custom_region_file)
            custom_region_file.columns = custom_region_file.columns.str.replace(" ", "")
            if len(custom_region_file.columns) > 3:
                # Ensures that any long formatted data will start with PrimaryProductID to begin the melt process
                custom_region_file.rename(
                    columns={custom_region_file.columns[0]: "PrimaryProductID"},
                    inplace=True,
                )
                custom_region_file = custom_region_file.melt(
                    id_vars="PrimaryProductID", var_name="Year", value_name="Ratio"
                )
                for i in custom_region_file.columns:
                    if custom_region_file[i].dropna().empty:
                        return render_template(
                            calulator_html_path,
                            error="Missing Column Data in File: Primary Product Ratios Column: "
                            + i,
                        )
                custom_region_file = custom_region_file.to_csv(index=False)
            else:
                custom_region_file.rename(
                    columns={
                        custom_region_file.columns[0]: "PrimaryProductID",
                        custom_region_file.columns[1]: "Year",
                        custom_region_file.columns[2]: "Ratio",
                    },
                    inplace=True,
                )
                for i in custom_region_file.columns:
                    if custom_region_file[i].dropna().empty:
                        return render_template(
                            calulator_html_path,
                            error="Missing Column Data in File: Primary Product Ratios Column: "
                            + i,
                        )
                custom_region_file = custom_region_file.to_csv(index=False)
    else:
        custom_region_file = ""
    end_use_product_ratios = request.files["EndUseRatiosFilename"]
    if end_use_product_ratios.filename != "":
        end_use_product_ratios = pd.read_csv(end_use_product_ratios)
        end_use_product_ratios.columns = end_use_product_ratios.columns.str.replace(
            " ", ""
        )
        if len(end_use_product_ratios.columns) > 3:
            # Ensures that any long formatted data will start with EndUseID to begin the melt process
            end_use_product_ratios.rename(
                columns={end_use_product_ratios.columns[0]: "EndUseID"}, inplace=True
            )
            end_use_product_ratios = end_use_product_ratios.melt(
                id_vars="EndUseID", var_name="Year", value_name="Ratio"
            )
            for i in end_use_product_ratios.columns:
                if end_use_product_ratios[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: End Use Product Ratios Column: "
                        + i,
                    )
            end_use_product_ratios = end_use_product_ratios.to_csv(index=False)
        else:
            end_use_product_ratios.rename(
                columns={
                    end_use_product_ratios.columns[0]: "EndUseID",
                    end_use_product_ratios.columns[1]: "Year",
                    end_use_product_ratios.columns[2]: "Ratio",
                },
                inplace=True,
            )
            for i in end_use_product_ratios.columns:
                if end_use_product_ratios[i].dropna().empty:
                    return render_template(
                        calulator_html_path,
                        error="Missing Column Data in File: End Use Product Ratios Column: "
                        + i,
                    )
            end_use_product_ratios = end_use_product_ratios.to_csv(index=False)

    if request.form.get("enduseproductrates"):
        end_use_product_rates = request.form["enduseproductrates"]

    dispositions = request.files["DispositionsFilename"]
    disposition_half_lives = request.files["DispositionHalfLivesFilename"]
    burned_ratios = request.files["BurnedRatiosFilename"]
    mbf_to_ccf = request.files["MbfToCcfFilename"]
    loss_factor = request.form["lossfactor"]
    temp_loss_factor = float(loss_factor)
    temp_loss_factor = temp_loss_factor / 100.0
    loss_factor = str(temp_loss_factor)
    iterations = request.form["iterations"]
    email = request.form["email"]
    run_name = request.form["runname"]
    run_name = run_name.replace(" ", "_")

    now = datetime.now()
    dt_string = now.strftime("%d-%m-%YT%H:%M:%S")
    new_id = str(run_name + "-" + dt_string)
    # The data is compiled to a dictionary to be processed with the S3Helper class
    data = {
        "harvest_data.csv": yearly_harvest_input,
        "harvest_data_type": harvest_data_type,
        "timber_product_ratios.csv": timber_product_ratios,
        "region": region_selection,
        "primary_product_ratios.csv": custom_region_file,
        "end_use_product_ratios.csv": end_use_product_ratios,
        "decay_function": end_use_product_rates,
        "discard_destinations.csv": dispositions,
        "discard_destination_ratios.csv": disposition_half_lives,
        # "distribution_data.csv":distribution_data,
        "discard_burned_w_energy_capture.csv": burned_ratios,
        "mbf_to_ccf_conversion.csv": mbf_to_ccf,
        "end_use_loss_factor": loss_factor,
        "iterations": iterations,
        "email": email,
        "scenario_name": run_name,
        "simulation_date": dt_string,
        "start_year": start_year,
        "end_year": stop_year,
        "user_string": new_id,
    }

    S3Helper.upload_input_group(
        HWPC_INPUT_BUCKET, user_data_folder + new_id + "/", data
    )
    return render_template("pages/submit.html")


@app.route("/submit")
def submit():
    return render_template("pages/submit.html", session=session.get("user"))


@app.route("/set-official", methods=["GET"])
def set_official():
    p = request.args.get("p")
    data_json = S3Helper.download_file("hwpc", user_data_folder + p + user_json_path)
    deliver_json = {}
    with open(data_json.name, "r+") as f:
        data = json.load(f)
        data["is_official_record"] = "true"
        deliver_json = json.dumps(data)
    deliver_json = deliver_json.encode()
    user_file = tempfile.TemporaryFile()
    user_file.write(deliver_json)
    user_file.seek(0)
    S3Helper.upload_file(user_file, "hwpc", user_data_folder + p + user_json_path)
    user_file.close()


@app.route("/output", methods=["GET"])
@login_required
def output():
    is_single = "false"
    p = request.args.get("p")
    q = request.args.get("q")
    y = request.args.get("y")
    print(p)
    print(q)
    data_dict = {}
    if y == None:
        user_json = S3Helper.download_file(
            HWPC_INPUT_BUCKET, user_data_folder + p + user_json_path
        )
        user_json = json.dumps(user_json.read().decode("utf-8"))

        user_zip = S3Helper.read_zipfile(
            HWPC_OUTPUT_BUCKET, user_data_output_folder + p + "/results/" + q + ".zip"
        )
        for file in user_zip:
            if ".csv" in file and "results" not in file:
                csv_string_io = StringIO(user_zip[file])
                test = pd.read_csv(csv_string_io, sep=",", header=0)
                try:
                    test = test.drop(columns="DiscardDestinationID")
                except Exception as e:
                    print("Missing Discard Destination ID: " + str(e))
                test.drop(test.tail(1).index, inplace=True)
                test = test.loc[:, ~test.columns.str.contains("^Unnamed")]
                data_dict[file[:-4]] = test.to_csv(index=False)

        data_json = json.dumps(data_dict)

        data_json = data_json.replace('\\"', " ")
    if y != None:
        print("years: " + y)
        is_single = "true"

        user_json = S3Helper.download_file(
            HWPC_INPUT_BUCKET, user_data_folder + p + user_json_path
        )
        user_json = json.dumps(user_json.read().decode("utf-8"))

        user_zip = S3Helper.read_zipfile(
            HWPC_OUTPUT_BUCKET,
            user_data_output_folder + p + "/results/" + y + "_" + q + ".zip",
        )

        for file in user_zip:
            if ".csv" in file and y in file and "results" not in file:
                print(file[:-4])
                csv_string_io = StringIO(user_zip[file])
                test = pd.read_csv(csv_string_io, sep=",", header=0)
                try:
                    print("Missing Discard Destination ID: " + str(e))
                    test = test.drop(columns="DiscardDestinationID")
                except Exception as e:
                    print(str(e))
                    data_dict[file[5:-4]] = test.to_csv(index=False)
        data_json = json.dumps(data_dict)

        data_json = data_json.replace('\\"', " ")

    return render_template(
        "pages/output.html",
        data_json=data_json,
        bucket=p,
        file_name=q,
        is_single=is_single,
        scenario_json=user_json,
        email_info=session["email"],
    )


@app.errorhandler(OAuthError)
def handle_error(error):
    return render_template("error.html", error=error)


@app.errorhandler(404)
def page_not_found(error):
    return render_template("pages/404.html", title="404"), 404


@app.errorhandler(Exception)
def handle_exception(e):
    # pass through HTTP errors
    if isinstance(e, HTTPException):
        return e

    # now you're handling non-HTTP exceptions only
    return render_template("pages/500.html", e=e), 500


if __name__ == "__main__":

    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.

    # Flask's development server will automatically serve static files in
    # the "static" directory. See:
    # http://flask.pocoo.org/docs/1.0/quickstart/#static-files. Once deployed,
    # App Engine itself will serve those files as configured in app.yaml.
    app.run(host="0.0.0.0", port=PORT, debug=FLASK_DEBUG)
