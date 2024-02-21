import argparse
import asyncio
import os
import logging
import requests
import threading
import sys

from concurrent.futures import Future
from ctod.core import utils
from ctod.core.layer import generate_layer_json
from ctod.core.tile_cache import get_tile_from_disk, save_tile_to_disk

# from ctod.server.server import app

from morecantile import TileMatrixSet
from uvicorn import Config, Server


def setup_logging(log_level=logging.INFO):
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_layer_json(tms: TileMatrixSet, filepath: str, max_zoom: int = 22) -> dict:
    json_string = generate_layer_json(tms, filepath, max_zoom)
    return json_string


def create_cache_folder(filepath: str):
    if not os.path.exists(filepath):
        os.makedirs(filepath)


def get_tile_range(layer_json: dict, zoom: int):
    available = layer_json["available"]
    info = available[zoom][0]
    return (info["startX"], info["endX"], info["startY"], info["endY"])


def seed_cache(
    server: Server,
    tms: TileMatrixSet,
    input_filepath: str,
    output_filepath: str,
    meshing_method: str,
    params: str,
    zoom_levels: list,
    overwrite: bool,
    done_future: Future,
):
    create_cache_folder(output_filepath)
    layer_json = get_layer_json(tms, input_filepath)

    for zoom in zoom_levels:
        if server.should_exit:  # Check if the server has been stopped
            break
        generate_level(
            server,
            tms,
            input_filepath,
            output_filepath,
            layer_json,
            zoom,
            meshing_method,
            params,
            overwrite,
        )

    logging.info(f"Finished seeding cache, stopping...")
    done_future.set_result(None)


def generate_level(
    server: Server,
    tms: TileMatrixSet,
    input_filepath: str,
    output_filepath: str,
    layer_json: dict,
    zoom: int,
    meshing_method: str,
    params: str,
    overwrite: bool,
):
    tile_range = get_tile_range(layer_json, zoom)
    tms_y_max = tms.minmax(zoom)["y"]["max"]

    x_range = range(tile_range[0], tile_range[1] + 1)
    y_range = range(tile_range[2], tile_range[3] + 1)

    logging.info(
        f"Generating cache for zoom level {zoom} with {len(x_range) * len(y_range)} tile(s)"
    )

    for x in x_range:
        if server.should_exit:  # Check if the server has been stopped
            break
        for y in y_range:
            if server.should_exit:  # Check if the server has been stopped
                break
            generate_tile(
                input_filepath,
                output_filepath,
                tms_y_max,
                x,
                y,
                zoom,
                meshing_method,
                params,
                overwrite,
            )


def generate_tile(
    input_filepath: str,
    output_filepath: str,
    tms_y_max: int,
    x: int,
    y: int,
    z: int,
    meshing_method: str,
    params: str,
    overwrite: bool,
):
    # If overwrite is false, skip generating and caching the tile
    if not overwrite:
        cached_tile = get_tile_from_disk(
            output_filepath, input_filepath, meshing_method, params, z, x, y
        )
        if cached_tile is not None:
            return

    tile_url = f"http://localhost:5580/tiles/{z}/{x}/{y}.terrain?cog={input_filepath}&skipCache=true&meshingMethod={meshing_method}"
    if params is not None:
        tile_url += f"&{params}"

    headers = {"Accept": "application/vnd.quantized-mesh;extensions=octvertexnormals"}
    response = requests.get(tile_url, headers=headers)
    if response.status_code == 200:
        tile_data = response.content
        y = tms_y_max - y
        save_tile_to_disk(
            output_filepath, input_filepath, meshing_method, z, x, y, tile_data
        )
    else:
        logging.error(
            f"Failed to generate tile {x} {y} {z}. Status code: {response.status_code}"
        )

async def clear_tasks():
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    
async def main():
    try:
        # Clear all arguments except the script name
        sys.argv = []

        os.environ["CTOD_UNSAFE"] = "false"
        config = Config(
            "ctod.server.server:app",
            host="0.0.0.0",
            port=5580,
            log_config=None,
            reload=False,
            workers=1,
        )
        server = Server(config)

        loop = asyncio.get_event_loop()
        server_task = loop.create_task(server.serve())
        await asyncio.sleep(2)

        done_future = Future()
        threading.Thread(
            target=seed_cache,
            args=(
                server,
                tms,
                args.input,
                args.output,
                args.meshing_method,
                args.params,
                zoom_levels,
                args.overwrite,
                done_future,
            ),
        ).start()

        # Wait for the done_future to be set
        while not done_future.done():
            await asyncio.sleep(0.1)  # Sleep for a short period to prevent busy waiting

        # Once the done_future is set, stop the server
        server.should_exit = True
        await server_task  # Wait for the server task to finish

        await clear_tasks()
        
    except KeyboardInterrupt:
        # On KeyboardInterrupt, stop the server and cancel all running tasks
        server.should_exit = True
        await clear_tasks()
        
    finally:
        loop.stop()


if __name__ == "__main__":
    setup_logging()
    parser = argparse.ArgumentParser(description="Seed the cache")
    parser.add_argument(
        "-i",
        "--input",
        metavar="input_dataset",
        required=True,
        help="input dataset, can be a cog, vrt or mosaic, make sure the path/url is exactly the same as the one being supplied to the server when requesting tiles.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="output_folder",
        default="./cache",
        help="Specify the output folder for the cache.",
    )
    parser.add_argument(
        "-m",
        "--meshing-method",
        metavar="meshing_method",
        default="grid",
        help="The meshing method to use: grid, delatin, martini. Defaults to grid.",
    )
    parser.add_argument(
        "-z",
        "--zoom-levels",
        metavar="zoom_levels",
        required=True,
        help="The zoom levels to create a cache for. Separate multiple levels with '-'.",
    )
    parser.add_argument(
        "-p",
        "--params",
        metavar="request_parameters",
        required=False,
        default=None,
        help="Pass options to tile requests, e.g. 'resamplingMethod=bilinear&defaultGridSize=20'. Defaults to None.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Add --overwrite to overwrite existing tiles in the cache. Defaults to False.",
    )

    args = parser.parse_args()
    tms = utils.get_tms()
    zoom_levels = list(map(int, args.zoom_levels.split("-")))

    logging.info(f"Creating cache for {args.input} at zoom levels {zoom_levels}")

    asyncio.run(main())
