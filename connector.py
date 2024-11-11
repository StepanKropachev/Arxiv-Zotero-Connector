import arxiv
import aiohttp
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
from functools import lru_cache
from pathlib import Path
from pyzotero import zotero
from typing import List, Dict, Optional, Tuple
import logging
import os
import pytz
import requests
import re
import unicodedata

from arxiv_config import ARXIV_TO_ZOTERO_MAPPING
from metadata_config import MetadataMapper
from search_params import ArxivSearchParams

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('arxiv_zotero.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class ZoteroAPIError(Exception):
    """Custom exception for Zotero API errors"""
    pass

@lru_cache(maxsize=32)
def load_credentials(env_path: str = None) -> dict:
    """Load credentials from environment variables or .env file with caching"""
    try:
        if env_path and not os.path.exists(env_path):
            raise FileNotFoundError(f"Environment file not found: {env_path}")
        
        env_locations = [
            loc for loc in [
                env_path if env_path else None,
                '.env',
                Path.home() / '.arxiv-zotero' / '.env',
                Path('/etc/arxiv-zotero/.env')
            ] if loc and os.path.exists(loc)
        ]
        
        if env_locations:
            load_dotenv(env_locations[0])
            logger.info(f"Loaded environment from {env_locations[0]}")
        
        required_vars = ['ZOTERO_LIBRARY_ID', 'ZOTERO_API_KEY']
        credentials = {var: os.getenv(var) for var in required_vars}
        
        if None in credentials.values():
            missing = [k for k, v in credentials.items() if v is None]
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
            
        return {
            'library_id': credentials['ZOTERO_LIBRARY_ID'],
            'api_key': credentials['ZOTERO_API_KEY'],
            'collection_key': os.getenv('COLLECTION_KEY')
        }
    except Exception as e:
        logger.error(f"Error loading credentials: {str(e)}")
        raise

class ArxivZoteroCollector:
    def __init__(self, zotero_library_id: str, zotero_api_key: str, collection_key: str = None):
        """Initialize the collector with API clients and configurations"""
        self.zot = zotero.Zotero(zotero_library_id, 'user', zotero_api_key)
        self.collection_key = collection_key
        self.download_dir = Path.home() / 'Downloads' / 'arxiv_papers'
        self.metadata_mapper = MetadataMapper(ARXIV_TO_ZOTERO_MAPPING)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
        self.session = requests.Session()
        self.session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=20
        ))
        
        self.async_session = None
        self.request_times = deque(maxlen=10)
        self.min_request_interval = 0.1
        
        if collection_key:
            self._validate_collection()

    def _validate_collection(self):
        """Validate collection existence"""
        try:
            collections = self.zot.collections()
            if not any(col['key'] == self.collection_key for col in collections):
                raise ValueError(f"Collection {self.collection_key} does not exist")
            logger.info(f"Successfully validated collection {self.collection_key}")
        except Exception as e:
            logger.error(f"Failed to validate collection {self.collection_key}: {str(e)}")
            raise

    def create_zotero_item(self, paper: Dict) -> Optional[str]:
        """Create a new item in Zotero using the metadata mapper"""
        try:
            template = self.zot.item_template('journalArticle')
            mapped_data = self.metadata_mapper.map_metadata(paper)
            template.update(mapped_data)

            response = self.zot.create_items([template])
            
            if 'successful' in response and response['successful']:
                item_key = list(response['successful'].values())[0]['key']
                logger.info(f"Successfully created item with key: {item_key}")
                return item_key
            else:
                logger.error(f"Failed to create Zotero item. Response: {response}")
                return None

        except Exception as e:
            logger.error(f"Error creating Zotero item: {str(e)}")
            return None

    def add_to_collection(self, item_key: str) -> bool:
        """Add an item to the specified collection"""
        if not self.collection_key:
            return True

        try:
            item = self.zot.item(item_key)
            success = self.zot.addto_collection(self.collection_key, item)
            
            if success:
                logger.info(f"Successfully added item {item_key} to collection")
                return True
            else:
                logger.error(f"Failed to add item {item_key} to collection")
                return False

        except Exception as e:
            logger.error(f"Error adding to collection: {str(e)}")
            return False

    def filter_by_date(self, result: arxiv.Result, start_date: Optional[datetime], end_date: Optional[datetime]) -> bool:
        """Filter arxiv result by date range"""
        if not (start_date or end_date):
            return True
            
        pub_date = result.published.astimezone(pytz.UTC)
        
        if start_date and pub_date < start_date:
            return False
        if end_date and pub_date > end_date:
            return False
            
        return True

    def filter_by_content_type(self, result: arxiv.Result, content_type: Optional[str]) -> bool:
        """Filter arxiv result by content type"""
        if not content_type:
            return True
            
        comment = getattr(result, 'comment', '') or ''
        journal_ref = getattr(result, 'journal_ref', '') or ''
        
        comment = comment.lower()
        journal_ref = journal_ref.lower()
        
        if content_type == 'journal':
            return bool(journal_ref and not ('preprint' in journal_ref or 'submitted' in journal_ref))
        elif content_type == 'conference':
            return bool('conference' in comment or 'proceedings' in comment or 
                    'conference' in journal_ref or 'proceedings' in journal_ref)
        elif content_type == 'preprint':
            return not bool(journal_ref)
            
        return True

    async def _prepare_arxiv_metadata(self, result: arxiv.Result) -> Optional[Dict]:
        """Prepare metadata from arxiv result"""
        try:
            return {
                'title': result.title,
                'abstract': result.summary,
                'authors': [author.name for author in result.authors],
                'published': result.published.strftime('%Y-%m-%d') if isinstance(result.published, datetime) else result.published,
                'arxiv_id': result.entry_id.split('/')[-1],
                'arxiv_url': result.entry_id,
                'pdf_url': result.pdf_url,
                'primary_category': result.primary_category,
                'categories': result.categories,
                'journal_ref': getattr(result, 'journal_ref', None),
                'doi': getattr(result, 'doi', None),
                'comment': getattr(result, 'comment', None)
            }
        except Exception as e:
            logger.error(f"Error preparing arxiv metadata: {str(e)}")
            return None

    async def _process_paper_async(self, paper: Dict, download_pdfs: bool = True) -> bool:
        """Process a single paper asynchronously"""
        try:
            # Create Zotero item
            item_key = self.create_zotero_item(paper)
            if not item_key:
                return False

            # Add to collection if specified
            if self.collection_key and not self.add_to_collection(item_key):
                return False

            # Download PDF if requested
            if download_pdfs:
                # Generate filename from title only
                filename = f"{self._sanitize_filename(paper['title'])}.pdf"
                pdf_path = self.download_dir / filename
                
                # If file already exists, add a number suffix
                counter = 1
                while pdf_path.exists():
                    filename = f"{self._sanitize_filename(paper['title'])} ({counter}).pdf"
                    pdf_path = self.download_dir / filename
                    counter += 1
                
                if not await self._download_pdf_async(paper['pdf_url'], pdf_path):
                    return False

            return True

        except Exception as e:
            logger.error(f"Error processing paper: {str(e)}")
            return False
        
    async def _download_pdf_async(self, url: str, path: Path) -> bool:
        """Download PDF asynchronously"""
        if not self.async_session:
            self.async_session = aiohttp.ClientSession()

        try:
            async with self.async_session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    path.write_bytes(content)
                    logger.info(f"Successfully downloaded PDF to {path}")
                    return True
                else:
                    logger.error(f"Failed to download PDF. Status: {response.status}")
                    return False

        except Exception as e:
            logger.error(f"Error downloading PDF: {str(e)}")
            return False

    def _sanitize_filename(self, title: str, max_length: int = 100) -> str:
        """
        Convert paper title to a safe filename while preserving original casing.
        
        Args:
            title: Paper title to convert
            max_length: Maximum length of the resulting filename
                
        Returns:
            str: Sanitized filename
        """
        # Normalize unicode characters
        filename = unicodedata.normalize('NFKD', title).encode('ASCII', 'ignore').decode()
        
        # Replace non-alphanumeric characters with spaces, preserving case
        filename = re.sub(r'[^\w\s-]', ' ', filename)
        
        # Replace multiple spaces with single space and strip
        filename = ' '.join(filename.split())
        
        # Truncate if too long, but try to break at word boundary
        if len(filename) > max_length:
            filename = filename[:max_length]
            last_space = filename.rfind(' ')
            if last_space > max_length * 0.8:  # Only truncate at space if it's not too short
                filename = filename[:last_space]
        
        # Replace problematic characters (if any remain) with safe alternatives
        filename = filename.replace('/', '-').replace('\\', '-')
        
        return filename

    def search_arxiv(self, search_params: ArxivSearchParams) -> List[Dict]:
        """Search arXiv using provided search parameters"""
        try:
            query = search_params.build_query()
            logger.info(f"Executing arXiv search with query: {query}")
            
            search = arxiv.Search(
                query=query,
                max_results=search_params.max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate
            )
            
            papers = []
            client = arxiv.Client(
                page_size=100,
                delay_seconds=3,
                num_retries=5
            )
            
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                
                for result in client.results(search):
                    if not self.filter_by_date(result, search_params.start_date, search_params.end_date):
                        continue
                        
                    if not self.filter_by_content_type(result, search_params.content_type):
                        continue
                    
                    future = executor.submit(
                        asyncio.run,
                        self._prepare_arxiv_metadata(result)
                    )
                    futures.append(future)
                
                for future in as_completed(futures):
                    paper_metadata = future.result()
                    if paper_metadata:
                        papers.append(paper_metadata)
            
            logger.info(f"Found {len(papers)} papers matching the search criteria")
            return papers
            
        except Exception as e:
            logger.error(f"Error searching arXiv: {str(e)}")
            return []

    async def run_collection_async(self, search_params: ArxivSearchParams, download_pdfs: bool = True) -> Tuple[int, int]:
        """Run collection process asynchronously using search parameters"""
        try:
            papers = self.search_arxiv(search_params)
            logger.info(f"Found {len(papers)} papers matching the criteria")
            
            if not papers:
                return 0, 0
                
            successful = 0
            failed = 0
            
            async def process_paper(paper):
                nonlocal successful, failed
                try:
                    if await self._process_paper_async(paper, download_pdfs):
                        successful += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Error processing paper: {str(e)}")
                
            tasks = [process_paper(paper) for paper in papers]
            await asyncio.gather(*tasks)
                    
            logger.info(f"Collection complete. Successfully processed {successful} papers. Failed: {failed}")
            return successful, failed
            
        except Exception as e:
            logger.error(f"Error in run_collection: {str(e)}")
            return 0, 0

    async def close(self):
        """Cleanup resources"""
        if self.session:
            self.session.close()
        if self.async_session:
            await self.async_session.close()

    def __del__(self):
        """Cleanup resources on deletion"""
        if self.session:
            self.session.close()

async def main():
    collector = None
    try:
        credentials = load_credentials()
        collector = ArxivZoteroCollector(
            zotero_library_id=credentials['library_id'],
            zotero_api_key=credentials['api_key'],
            collection_key=credentials['collection_key']
        )
        
        # Example usage with ArxivSearchParams
        search_params = ArxivSearchParams(
            keywords=["multi-agent systems"],
            max_results=10,
            categories=["cs.AI"]
        )
        
        successful, failed = await collector.run_collection_async(
            search_params=search_params,
            download_pdfs=True
        )
        
        logger.info(f"Script completed. Successfully processed: {successful}, Failed: {failed}")
        
    except Exception as e:
        logger.error(f"Script failed: {str(e)}")
    finally:
        if collector:
            await collector.close()

if __name__ == "__main__":
    asyncio.run(main())