from io import BytesIO
from flask import Flask, render_template, request, make_response, send_file, send_from_directory, flash, redirect, url_for
from flask_caching import Cache
from flask_paginate import Pagination, get_page_args
import pandas as pd
from app.CoreB.invoices_list import bp
from app import login_required
from app.models import Invoice
from app import db
from datetime import datetime
from app.pdfwriter import PdfWriter
from app.reader import Reader
import numpy as np

app = Flask(__name__)
cache1 = Cache(app, config={'CACHE_TYPE': 'simple'}) # Memory-based cache
defaultCache = Cache(app, config={'CACHE_TYPE': 'simple'})

pdfWriter = PdfWriter("app/static/invoice-base.pdf","app/static/filled-out-v2.pdf")
services_reader = Reader("services.csv")  # services is a file to list all available services with their prices
services_no_price_reader = Reader("services_with_no_unit_price.csv")  # this file is to list all services that their price is not calculated based on samples

def list_services(services_str, services_to_find):
    """
    Find what services are requested

    services_str (str): string with services requested
    services_to_find (list(str)): list of possible services to find

    return (list(str)): list of services found 
    """
    services = []

    for service_to_find in services_to_find:
        if services_str.__contains__(service_to_find["Service"]):
            services.append(service_to_find)
    
    return services

@bp.route('/invoice', methods=['POST'])
@login_required(role=["admin", "coreB"])
def invoice():
    """
    POST: Provide data to generate invoice
    """
    if request.method == 'POST':
        # get order data to input automatically into invoice (passed to POST)
        order_num = request.form.get('order_num')
        pi_name = request.form.get('pi_name')
        bm_info = request.form.get('bm_info')
        service_type = request.form.get('service_type')
        services_str = request.form.get('services')
        sample_num = request.form.get('sample_num')
        # get account number and manager name from bm_info field (format: acc_num,additional info)
        bm_info_split = bm_info.split(",")

        # check if bm info format is correct
        if len(bm_info_split) != 3:
            return render_template('CoreB/error_invoice.html', error_msg="Please correct Account number and billing contact person format (account number, manager name, phone number)")
        acc_num = bm_info_split[0]
        manager_name = bm_info_split[1]

        # check what services are selected and put them into array       
        # if service is BioRender license then make service_str the same as service_type to work the same way as the other servies
        if service_type == "BioRender license":
            biorender_accounts = services_str
            services_str = service_type
            # pass dict with hidden data just to pass it to the next request
            hidden_data = {
                "Account Number": acc_num,
                "Quantity" : sample_num,
                "Order Number": order_num,
                "Manager Name": manager_name,
                "PI Name": pi_name,
                "BioRender Accounts": biorender_accounts
            }
        else:
            # pass dict with hidden data just to pass it to the next request
            hidden_data = {
                "Account Number": acc_num,
                "Quantity" : sample_num,
                "Order Number": order_num,
                "Manager Name": manager_name,
                "PI Name": pi_name
            }
        services_to_find_data = services_reader.getRawDataCSV(dict=True)
        services_data = list_services(services_str, services_to_find_data)
        print(f"services_to_find_data: {services_to_find_data}")

        # get invoice data from DB for each service or make a record if none exists
        invoices = []
        total_price_sum = 0.0
        for service_data in services_data:
            invoice = Invoice.query.filter_by(project_id = order_num, service_type=service_data["Service"]).first()
            if invoice == None:
                invoice = Invoice(project_id = order_num, 
                                service_type = service_data["Service"],
                                service_sample_number = 0.0,
                                service_sample_price = float(service_data["Price"]), 
                                total_price = 0.0,
                                discount_sample_number = 0.0,
                                discount_sample_amount = 0.0,
                                discount_reason = "", 
                                total_discount = 0.0)
                db.session.add(invoice)
            invoices.append(invoice)
            total_price_sum += invoice.total_price

        invoice = Invoice.query.filter_by(project_id = order_num, service_type="All services discount").first()
        if invoice == None:
            invoice = Invoice(project_id = order_num, 
                            service_type = "All services discount",
                            service_sample_number = 0.0,
                            service_sample_price = 0.0, 
                            total_price = 0.0,
                            discount_sample_number = 0.0,
                            discount_sample_amount = 0.0,
                            discount_reason = "", 
                            total_discount = 0.0)
            
            db.session.add(invoice)
        invoices.append(invoice)
        db.session.commit()

        if total_price_sum == 0:
            percent_discount = 0
        else:
            percent_discount = (round(invoice.total_discount/total_price_sum)) * 100.0
        
        print(f"\ninvoice.total_discount: {invoice.total_discount}\n total_price_sum: {total_price_sum}")

        response = make_response(render_template('CoreB/edit_invoice.html', order_num = order_num, service_type = service_type, sample_num = sample_num, fields_hidden = hidden_data, invoices=invoices, percent_discount=percent_discount, len=len))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate" # HTTP 1.1.
        response.headers["Pragma"] = "no-cache" # HTTP 1.0.
        response.headers["Expires"] = "0" # Proxies.
        return response

@bp.route('/gen_invoice', methods=['POST'])
@login_required(role=["admin", "coreB"])
def gen_invoice(): 
    """
    POST: Generate invoice PDF file
    """
    if request.method == 'POST':
        # get order data to input automatically into invoice
        order_num = request.form.get("Order Number") or ""
        pi_name = request.form.get('PI Name') or ""
        pi_name_line = "Service for " + pi_name
        acc_num = request.form.get('Account Number') or ""
        manager_name = request.form.get('Manager Name') or ""
        services_num = request.form.get('Services Number') or 0
        biorender_accounts = request.form.get('BioRender Accounts') or ""

        # based on order data prepare inputs
        date = datetime.now().strftime('%m/%d/%Y')

        # dictionary of data to put into PDF
        dict_data = {
            'DEBIT ACCOUNTRow1': acc_num,
            'DEPT REQUISITION Row1': order_num,
            'Date5_af_date': date,
            'CARE OFRow1': manager_name,
            'DESCRIPTIONRow2': pi_name_line
        }

        # services details
        # initial row number values to start from in the invoice PDF
        service_row = 4
        item_number = 1
        # initial grand total prices
        grand_total_discount = 0.0
        grand_total = 0.0
        total_service_amount = 0.0
        # services to not caount per sample
        services_no_unit_price_data = services_no_price_reader.getRawDataCSV(dict=True)
        services_no_unit_price = [snop["Service"] for snop in services_no_unit_price_data]
        # loop all services
        for i in range(0, int(services_num)):
            # get needed values from invoice form, create variable names to match their names in html file
            get_name_key = "service " + str(i) + " name"
            get_qty_key = "service " + str(i) + " qty"
            get_discount_reason_key = "service " + str(i) + " discount reason"
            get_discount_qty_key = "service " + str(i) + " discount qty"
            get_discount_amount_key = "service " + str(i) + " discount amount"
            service_price_key = "service " + str(i) + " price"
            service_name_detail = request.form.get(get_name_key)
            service_qty_detail = request.form.get(get_qty_key)
            service_discount_reason_detail = request.form.get(get_discount_reason_key)
            service_discount_qty_detail = request.form.get(get_discount_qty_key)
            service_discount_amount_detail = request.form.get(get_discount_amount_key)
            service_price_detail = request.form.get(service_price_key)

            # if service is the last discount put it at the bottom of the PDF
            if service_name_detail == "All services discount":
                service_row = 21

            # Discount details keys
            item_discount_key = "ITEM Row" + str(service_row + 1)
            qty_discount_key = "QTYRow" + str(service_row + 1)
            unit_discount_key = "UNITRow" + str(service_row + 1) 
            service_discount_reason_key = "DESCRIPTIONRow" + str(service_row + 1)
            service_discount_amount_key = "UNIT COSTRow" + str(service_row + 1)
            service_discount_total_key = "TOTALRow" + str(service_row + 1)

            if service_name_detail != "All services discount":
                # Service details keys
                item_key = "ITEM Row" + str(service_row)
                qty_key = "QTYRow" + str(service_row)
                unit_key = "UNITRow" + str(service_row)
                service_name_key = "DESCRIPTIONRow" + str(service_row)
                service_amount_key = "UNIT COSTRow" + str(service_row)
                service_total_key = "TOTALRow" + str(service_row)

                # Service details values
                if service_price_detail == "":
                    return render_template('CoreB/error_invoice.html', error_msg="Service price must be provided")
                elif service_qty_detail == "":
                    return render_template('CoreB/error_invoice.html', error_msg="Service samples must be provided")
            
                service_price_detail = round(float(service_price_detail),1)
                dict_data[item_key] = str(item_number)
                dict_data[qty_key] = service_qty_detail
                dict_data[unit_key] = "ea"
                dict_data[service_name_key] = service_name_detail
                dict_data[service_amount_key] = "$ " + str(service_price_detail)
                if service_name_detail in services_no_unit_price:
                    total_service_amount = service_price_detail
                else:
                    total_service_amount = float(service_qty_detail) * service_price_detail
                grand_total += total_service_amount
                dict_data[service_total_key] = "$ " + str(total_service_amount)

            # var to insert total discount amount to DB
            total_discount_amount = 0.0

            if service_name_detail == "All services discount":
                service_discount_qty_detail = 1.0
                total_service_amount = 0.0

            # Discount details values
            if service_discount_reason_detail != None and len(service_discount_reason_detail) != 0:
                if service_discount_amount_detail == "":
                    return render_template('CoreB/error_invoice.html', error_msg="Discount amount must be provided")
                elif service_discount_qty_detail == "":
                    return render_template('CoreB/error_invoice.html', error_msg="Discounted sample number must be provided")
                service_discount_amount_detail = float(service_discount_amount_detail)
                dict_data[item_discount_key] = str(item_number+1)
                dict_data[qty_discount_key] = service_discount_qty_detail
                dict_data[unit_discount_key] = "ea"
                dict_data[service_discount_reason_key] = service_discount_reason_detail
                if service_name_detail == "All services discount":
                    service_discount_amount_detail = round(grand_total * (service_discount_amount_detail/100),1)
                dict_data[service_discount_amount_key] = "-$ " + str(service_discount_amount_detail)
                total_discount_amount = float(service_discount_qty_detail) * service_discount_amount_detail
                grand_total_discount += total_discount_amount
                dict_data[service_discount_total_key] = "-$ " + str(total_discount_amount)
                
            print(f"total_service_amount: {total_service_amount}")

            # change values of invoice record
            existing_invoice = Invoice.query.filter_by(project_id = order_num, service_type = service_name_detail).first()

            existing_invoice.service_sample_number = float(service_qty_detail)
            existing_invoice.service_sample_price = service_price_detail
            existing_invoice.total_price = total_service_amount
            existing_invoice.discount_sample_number = float(service_discount_qty_detail)
            existing_invoice.discount_sample_amount = service_discount_amount_detail
            existing_invoice.discount_reason = service_discount_reason_detail
            existing_invoice.total_discount = total_discount_amount
            db.session.commit()


            # increment row to put data info into
            service_row += 2
            item_number += 2

        # grand total price of service
        service_grand_total_key = "TOTALGRAND TOTAL"
        dict_data[service_grand_total_key] = "$ " + str(grand_total - grand_total_discount)
        print(f"Dictionary: {dict_data}")

        # list BioRender accounts
        if biorender_accounts:
            biorender_title_row = 6
            biorender_row = 7
            item_key = "DESCRIPTIONRow" + str(biorender_title_row)
            dict_data[item_key] = "License for"
            biorender_accounts_list = biorender_accounts.split(",")
            for biorender_account in biorender_accounts_list:
                item_key = "DESCRIPTIONRow" + str(biorender_row)
                dict_data[item_key] = str(biorender_account)
                biorender_row += 1
	
        pdfWriter.fillForm(dict_data)
        return send_from_directory('static', "filled-out-v2.pdf")

@bp.route('/invoices_list', methods=['GET', 'POST'])
@login_required(role=["coreB", "admin"])
def invoices_list():
    """
    GET: Display list of all invoices made
    POST: Display filtered list of all invoices made
    """
    with app.app_context():
        cache1.delete('cached_data')

    # get invoice list data from DB
    invoices = Invoice.query.all()

    # lists to sum data into
    data = []
    project_ids = []
    total_prices = []
    total_discounts = []

    # for every invoice sum values from single project together
    for invoice in invoices:
        invoice_project_id = invoice.project_id
        if invoice_project_id not in project_ids:
            project_ids.append(invoice_project_id)
            total_prices.append(invoice.total_price)
            total_discounts.append(invoice.total_discount)
        else:
            total_prices[project_ids.index(invoice_project_id)] += invoice.total_price
            total_discounts[project_ids.index(invoice_project_id)] += invoice.total_discount

    final_prices = np.array(total_prices) - np.array(total_discounts)

    # for each project ID get data into list of dicts to display
    for p_id in range(0, len(project_ids)):
        invoice_dict = {}
        invoice_dict["Project ID"] = project_ids[p_id]
        invoice_dict["Total price"] = total_prices[p_id]
        invoice_dict["Total discount"] = total_discounts[p_id]
        invoice_dict["Final price"] = final_prices[p_id]
        data.append(invoice_dict)

    if request.method == 'POST':
        sort = request.form.get('sort') or "Original"
        # sort dict
        if sort != 'Original':
            if sort != 'Project ID':
                data = sorted(data, key=lambda d: d[sort], reverse=True)
            else:
                data = sorted(data, key=lambda d: d[sort])
    
    with app.app_context():
        cache1.set('cached_data', data, timeout=3600)

    page, per_page, offset = get_page_args(page_parameter='page', 
                                        per_page_parameter='per_page')
    total = len(data)

    pagination_users = data[offset: offset + per_page]
    pagination = Pagination(page=page, per_page=per_page, total=total)

    # use to prevent user from caching pages
    response = make_response(render_template("CoreB/invoices_list.html", data=pagination_users, page=page, per_page=per_page, pagination=pagination, list=list, len=len, str=str))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate" # HTTP 1.1.
    response.headers["Pragma"] = "no-cache" # HTTP 1.0.
    response.headers["Expires"] = "0" # Proxies.
    return response

@bp.route('/invoice_details', methods=['GET'])
@login_required(["coreB", "admin"])
def invoice_details():
    """
    GET: Delete invoice
    """
    if request.method == 'GET':
        project_id = request.args['project_id']

        invoice_details = []

        # get invoices with specified project id
        invoice = Invoice.query.filter_by(project_id=project_id).all()
        if invoice:
            for inv in invoice:
                invoice_detail_dict = {
                    'Service': inv.service_type,
                    'Total price': inv.total_price,
                    'Total discount': inv.total_discount
                }
                invoice_details.append(invoice_detail_dict)


        # use to prevent user from caching pages
        response = make_response(render_template('CoreB/invoice_details.html', data=invoice_details, project_id=project_id, list=list, len=len, str=str))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate" # HTTP 1.1.
        response.headers["Pragma"] = "no-cache" # HTTP 1.0.
        response.headers["Expires"] = "0" # Proxies.
        return response

@bp.route('/delete_invoice', methods=['GET'])
@login_required(["coreB", "admin"])
def delete_invoice():
    """
    GET: Delete invoice
    """
    if request.method == 'GET':
        project_id = request.args['project_id']

        # get invoices with specified project id
        invoice = Invoice.query.filter_by(project_id=project_id).all()
        if invoice:
            for inv in invoice:
                db.session.delete(inv)
            db.session.commit()
        
        response = make_response(redirect(url_for('invoices_list.invoices_list')))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate" # HTTP 1.1.
        response.headers["Pragma"] = "no-cache" # HTTP 1.0.
        response.headers["Expires"] = "0" # Proxies.
        return response

@bp.route('/downloadInvoicesCSV', methods=['GET'])
@login_required(role=["coreB"])
def downloadCSV():
    with app.app_context():
        saved_data = cache1.get('cached_data')
    
    if saved_data is None:
        with app.app_context():
            saved_data = defaultCache.get('cached_data')

    df = pd.DataFrame.from_dict(saved_data)
    csv = df.to_csv(index=False)
    
    # Convert the CSV string to bytes and use BytesIO
    csv_bytes = csv.encode('utf-8')
    csv_io = BytesIO(csv_bytes)
    
    return send_file(csv_io, mimetype='text/csv', as_attachment=True, download_name='Invoices.csv')
