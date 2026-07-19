def install_logs():
    coloredlogs.DEFAULT_FIELD_STYLES["levelname"]["color"] = "white"
    coloredlogs.install(fmt="%(asctime)s %(levelname)s %(module)s %(message)s")
