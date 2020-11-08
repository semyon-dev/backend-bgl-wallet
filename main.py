import pybgl
import requests

from flask import Flask, jsonify, request

app = Flask(__name__)
URL = 'http://bgl_user:12345678@161.35.123.34:8332'


@app.route("/wallet", methods=['POST'])
def create_wallet():
    entropy = pybgl.generate_entropy()
    mnemonic = pybgl.entropy_to_mnemonic(entropy)
    seed = pybgl.mnemonic_to_seed(mnemonic)

    x_private_key = pybgl.create_master_xprivate_key(seed)

    private_key = pybgl.private_from_xprivate_key(x_private_key)
    print(private_key)
    # wif_private_key = pybgl.private_key_to_wif(private_key)

    public_key = pybgl.private_to_public_key(private_key)
    hex_public_key = pybgl.private_to_public_key(private_key, True, True)

    address = pybgl.public_key_to_address(public_key)

    # Create wallet request

    payload = {
        "method": "createwallet",
        "params": [address, True],
        "jsonrpc": "2.0",
        "id": "backend",
    }
    response = requests.post(URL, json=payload)
    print(response.text)

    # Import public key to node
    url_request = URL + '/wallet/' + address
    payload = {
        "method": "importpubkey",
        "params": [hex_public_key, address, True],
        "jsonrpc": "2.0",
        "id": "backend",
    }
    response = requests.post(url_request, json=payload)
    print(response.text)

    reply = {'address': address, 'private_key': private_key,
             "public_key": hex_public_key, "mnemonic": mnemonic}
    return jsonify(reply)


@app.route("/balance/<address>", methods=['GET'])
def get_balance(address):
    assert address == request.view_args['address']

    payload = {
        "method": "getbalance",
        "params": ["*", 0, True],
        "jsonrpc": "2.0",
        "id": "backend",
    }
    response = requests.post(URL + '/wallet/' + address, json=payload).json()
    print(response)

    reply = {'amount': response["result"]}
    return jsonify(reply)


@app.route("/history", methods=['GET'])
def get_history():
    page = request.args.get('page')
    address = request.args.get('address')

    skip = int(page) * 25

    payload = {
        "method": "listtransactions",
        "params": ["*", 25, skip, True],
        "jsonrpc": "2.0",
        "id": "backend",
    }
    response = requests.post(URL + '/wallet/' + address, json=payload).json()

    back_txid = "tx"

    for i in response["result"]:

        if i["address"] == address and i["category"] == "send":
            back_txid = i["txid"]
            response["result"].remove(i)

        i["amount"] = str(i["amount"])
        if "bip125-replaceable" in i:
            i.pop("bip125-replaceable")
        if "blockhash" in i:
            i.pop("blockhash")
        if "blockheight" in i:
            i.pop("blockheight")
        if "blocktime" in i:
            i.pop("blocktime")
        if "blockindex" in i:
            i.pop("blockindex")
        if "walletconflicts" in i:
            i.pop("walletconflicts")
        if "involvesWatchonly" in i:
            i.pop("involvesWatchonly")
        i.pop("vout")

    for i in response["result"]:
        if i["address"] == address and i["category"] == "receive" and i["txid"] == back_txid:
            response["result"].remove(i)

    return jsonify(response["result"])


@app.route("/transaction", methods=['POST'])
def create_transaction():
    # Create transaction
    frontend = request.json
    print(frontend)
    message = "unknown error"
    try:
        # Get list of unspent
        addresses = [frontend["address"]]
        payload = {
            "method": "listunspent",
            "params": [0, 999999999, addresses],
            "jsonrpc": "2.0",
            "id": "backend",
        }
        response = requests.post(URL + '/wallet/' + frontend["address"], json=payload).json()
        print(response)
        if response["error"] is not None:
            message = response["error"]["message"]
            return jsonify({"message": message}), 400

        inputs = []
        outputs = []

        sum_amount = 0
        for i in response["result"]:
            sum_amount += i["amount"]
            i_append = {"txid": i["txid"],
                        "vout": i["vout"]}
            inputs.append(i_append)
            if sum_amount >= frontend["amount"] + 0.01:
                break

        print("sum_amount : ", sum_amount)
        print("sum_amount - frontend amount: ", sum_amount - frontend["amount"])
        print(sum_amount - frontend["amount"] - 0.01)
        back_output = {frontend["address"]: float(format(sum_amount - frontend["amount"] - 0.01, '.8f'))}
        to_output = {frontend["to_address"]: frontend["amount"]}

        outputs.append(back_output)
        outputs.append(to_output)

        payload = {
            "method": "createrawtransaction",
            "params": [inputs, outputs],
            "jsonrpc": "2.0",
            "id": "backend",
        }
        print(payload)
        response = requests.post(URL, json=payload).json()
        print(response)
        if response["error"] is not None:
            message = response["error"]["message"]
            return jsonify({"message": message}), 400

        hexstring = response["result"]
        print("hexstring == ", hexstring)
        private_key = [frontend["private_key"]]

        payload = {
            "method": "signrawtransactionwithkey",
            "params": [hexstring, private_key],
            "jsonrpc": "2.0",
            "id": "backend",
        }
        response = requests.post(URL, json=payload).json()
        print(response)
        if response["error"] is not None:
            message = response["error"]["message"]
            return jsonify({"message": message}), 400

        hexstring = response["result"]["hex"]

        payload = {
            "method": "sendrawtransaction",
            "params": [hexstring],
            "jsonrpc": "2.0",
            "id": "backend",
        }
        response = requests.post(URL, json=payload).json()
        print(response)
        if response["error"] is not None:
            message = response["error"]["message"]
            return jsonify({"message": message}), 400
    except:
        return jsonify({"message": message}), 400

    response["message"] = "Transaction was sent successfully"
    return jsonify(response), 201


if __name__ == "__main__":
    app.run()
