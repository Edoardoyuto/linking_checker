import os
import webbrowser
import json


def main():   
    import json
    with open('data/author_affiliation_dataset.jsonl' , 'r',encoding='utf-8') as f:
        jsonl_data = [json.loads(l) for l in f.readlines()]

    for data in jsonl_data:
        check_dataset(data['arxiv_id'] , jsonl_data)
        

    


def open_pdf_browse(paper_id):
    url = "https://arxiv.org/pdf/"+paper_id
    webbrowser.open(url)

def close_pdf_browse(paper_id):
    url = "https://arxiv.org/pdf/"+paper_id
    webbrowser.close(url)   

def check_dataset(paper_id , dataset):

    if not dataset:
        print("Dataset is empty.")
        return  
    
    for data in dataset:
        if data['arxiv_id'] == paper_id:
            flag = True
            print(json.dumps(data, indent=4))
            open_pdf_browse(paper_id)
            input("Press Enter to continue to the next paper...")
            print("\n" + "="*50 + "\n")
            close_pdf_browse(paper_id)
            break
    if not flag:
        print(f"Paper ID {paper_id} not found in the dataset.")    


if __name__ == "__main__":
    # Create the data directory if it doesn't exist
    if not os.path.exists("data"):
        os.makedirs("data")

    main()