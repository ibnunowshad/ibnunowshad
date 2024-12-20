import datetime
from dateutil import relativedelta
import requests
import os
from xml.dom import minidom
import time
import hashlib
import signal
from typing import Dict, List, Optional, Tuple, Union
import sys

# Personal access token with permissions: read:enterprise, read:org, read:repo_hook, read:user, repo
HEADERS = {'Authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
# OWNER_ID = os.environ['OWNER_ID']

def validate_date(date_str: str) -> bool:
    """Validates if a string is a proper ISO format date."""
    try:
        datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return True
    except (ValueError, AttributeError):
        return False

def validate_github_token(token: str) -> bool:
    """Validates if a GitHub token has the correct format."""
    if not token or not isinstance(token, str):
        print("Token validation failed: Token is empty or not a string")
        return False

    # More permissive validation for different token formats
    # Accepts both classic and fine-grained tokens
    is_valid = (
        isinstance(token, str) and
        len(token) >= 4 and  # Minimum reasonable length
        all(c.isalnum() or c in '_-' for c in token)
    )
    
    if not is_valid:
        print(f"Token validation failed: Length={len(token)}")
    
    return is_valid

def validate_github_username(username: str) -> bool:
    """Validates if a GitHub username follows GitHub's username rules."""
    if not username or not isinstance(username, str):
        return False
    # GitHub usernames: 1-39 chars, alphanumeric or single hyphens, no leading/trailing hyphen
    if len(username) > 39 or len(username) < 1:
        return False
    if username.startswith('-') or username.endswith('-'):
        return False
    if '--' in username:
        return False
    return all(c.isalnum() or c == '-' for c in username)

def validate_environment() -> None:
    """Validates all required environment variables are present and valid."""
    required_vars = {
        'ACCESS_TOKEN': validate_github_token,
        'USER_NAME': validate_github_username
    }
    
    # Debug logging
    for var in required_vars:
        value = os.environ.get(var)
        masked_value = '***' if value else 'None'
        print(f"Checking {var}: Present={bool(value)}, Length={len(value) if value else 0}")
    
    missing = []
    invalid = []
    
    for var, validator in required_vars.items():
        value = os.environ.get(var)
        if not value:
            missing.append(var)
        elif not validator(value):
            invalid.append(var)
    
    if missing or invalid:
        error_msg = []
        if missing:
            error_msg.append(f"Missing variables: {', '.join(missing)}")
        if invalid:
            error_msg.append(f"Invalid variables: {', '.join(invalid)}")
        raise EnvironmentError(' | '.join(error_msg))

def ensure_cache_directory():
    if not os.path.exists('cache'):
        os.makedirs('cache')

def daily_readme(birthday: str) -> str:
    """Returns the length of time since birth date."""
    if not validate_date(birthday):
        raise ValueError("Birthday must be in ISO format (YYYY-MM-DD)")
    
    try:
        birth_date = datetime.fromisoformat(birthday.replace('Z', '+00:00'))
        diff = relativedelta.relativedelta(datetime.today(), birth_date)
        return '{} {}, {} {}, {} {}{}'.format(
            diff.years, 'year' + format_plural(diff.years),
            diff.months, 'month' + format_plural(diff.months),
            diff.days, 'day' + format_plural(diff.days),
            ' 🎂' if (diff.months == 0 and diff.days == 0) else '')
    except Exception as e:
        raise ValueError(f"Error calculating age: {str(e)}")

def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''

def simple_request(func_name: str, query: str, variables: Dict) -> requests.Response:
    """Makes a GitHub GraphQL API request with validation."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must be a non-empty string")
    if not isinstance(variables, dict):
        raise ValueError("Variables must be a dictionary")
    if not isinstance(func_name, str) or not func_name.strip():
        raise ValueError("Function name must be a non-empty string")
    
    try:
        request = requests.post(
            'https://api.github.com/graphql',
            json={'query': query, 'variables': variables},
            headers=HEADERS,
            timeout=10  # Add timeout
        )
        
        if request.status_code == 200:
            return request
        elif request.status_code == 401:
            raise ValueError("Invalid GitHub token")
        elif request.status_code == 403:
            raise ValueError("Rate limit exceeded or lacking permissions")
        else:
            raise Exception(f"{func_name} failed: {request.status_code}, {request.text}")
            
    except requests.RequestException as e:
        raise ConnectionError(f"Failed to connect to GitHub API: {str(e)}")

def graph_commits(start_date: str, end_date: str) -> int:
    """Fetches commit count with date validation."""
    if not validate_date(start_date):
        raise ValueError("start_date must be in ISO format (YYYY-MM-DD)")
    if not validate_date(end_date):
        raise ValueError("end_date must be in ISO format (YYYY-MM-DD)")
    
    if datetime.fromisoformat(start_date.replace('Z', '+00:00')) > datetime.fromisoformat(end_date.replace('Z', '+00:00')):
        raise ValueError("start_date cannot be after end_date")

    query_count('graph_commits')
    query = """
    query ($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }
    """
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        stargazers {
                            totalCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }
    """
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        return stars_counter(request.json()['data']['user']['repositories']['edges'])

def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: 
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars

def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = """
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    committedDate
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }
    """
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] is not None:  # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0
    force_close_file(data, cache_comment)  # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time!\nYou've hit the non-documented anti-abuse limit!")
    raise Exception(f"recursive_loc() has failed with a {request.status_code}, {request.text}, {QUERY_COUNT}")

def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        if node['node']['author']['user']['id'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])

def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }
    """
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:  # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']  # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)

def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True  # Assume all repositories are cached
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'  # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            for line in f:
                if line.split(' ')[0] == 'data':
                    data = int(line.split(' ')[1])
                    cache_comment = int(line.split(' ')[2])
                    if (comment_size > 0 and cache_comment != comment_size) or force_cache:  # If the cache is invalid
                        os.remove(filename)
                        cached = False
                        break
    except FileNotFoundError:
        cached = False
    if not cached:  # If any repository is not cached
        with open(filename, 'w') as f:
            for i in edges:
                if i['node']['defaultBranchRef'] is not None:
                    data = int(time.mktime(time.strptime(i['node']['defaultBranchRef']['target']['history']['edges'][0]['node']['committedDate'], '%Y-%m-%dT%H:%M:%SZ')))
                    cache_comment = comment_size
                    f.write('data {} {}\n'.format(data, cache_comment))
                    addition_total, deletion_total, my_commits = recursive_loc(USER_NAME, i['node']['nameWithOwner'].split('/')[1], data, cache_comment)
                    loc_add += addition_total
                    loc_del += deletion_total
        with open(filename, 'w') as f:  # Save the file in case the program crashes again
            f.write('data {} {}\n'.format(data, cache_comment))
            f.write('comment_size {} {}\n'.format(comment_size, loc_add - loc_del))
            return loc_add - loc_del
    else:
        with open(filename, 'r') as f:
            for line in f:
                if line.split(' ')[0] == 'comment_size':
                    return int(line.split(' ')[2])

def svg_overwrite(
    filename: str,
    age_data: str,
    commit_data: str,
    star_data: str,
    repo_data: str,
    contrib_data: str,
    follower_data: str,
    loc_data: Tuple[str, str, str]
) -> None:
    """Updates SVG file with validation."""
    if not os.path.exists(filename):
        raise FileNotFoundError(f"SVG file not found: {filename}")
    if not filename.lower().endswith('.svg'):
        raise ValueError("File must be an SVG file")
    
    # Validate all data parameters are strings
    params = {
        'age_data': age_data,
        'commit_data': commit_data,
        'star_data': star_data,
        'repo_data': repo_data,
        'contrib_data': contrib_data,
        'follower_data': follower_data
    }
    
    for param_name, param_value in params.items():
        if not isinstance(param_value, str):
            raise ValueError(f"{param_name} must be a string")
    
    if not isinstance(loc_data, (tuple, list)) or len(loc_data) != 3:
        raise ValueError("loc_data must be a tuple/list of 3 strings")
    
    try:
        svg = minidom.parse(filename)
        tspan = svg.getElementsByTagName('tspan')
        
        # Validate that all required indices exist
        required_indices = [30, 65, 67, 69, 71, 73, 75, 76, 77]
        max_index = len(tspan) - 1
        
        if max_index < max(required_indices):
            raise ValueError(f"SVG file doesn't have enough tspan elements. Found {max_index + 1}, need {max(required_indices) + 1}")
        
        tspan[30].firstChild.data = age_data
        tspan[65].firstChild.data = repo_data
        tspan[67].firstChild.data = contrib_data
        tspan[69].firstChild.data = commit_data
        tspan[71].firstChild.data = star_data
        tspan[73].firstChild.data = follower_data
        tspan[75].firstChild.data = loc_data[2]
        tspan[76].firstChild.data = loc_data[0] + '++'
        tspan[77].firstChild.data = loc_data[1] + '--'
        f.write(svg.toxml('utf-8').decode('utf-8'))
        f.close()
    except Exception as e:
        raise ValueError(f"Failed to process SVG file: {str(e)}")

def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'  # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size]  # save the comment block
    data = data[comment_size:]  # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits

def svg_element_getter(filename):
    """
    Prints the element index of every element in the SVG file
    """
    svg = minidom.parse(filename)
    open(filename, mode='r', encoding='utf-8')
    tspan = svg.getElementsByTagName('tspan')
    for index in range(len(tspan)):
        print(index, tspan[index].firstChild.data)

def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = """
    query ($login: String!) {
        user(login: $login) {
            id
            createdAt
        }
    }
    """
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = """
    query ($login: String!) {
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }
    """
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])

def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1

def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start

def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return

def force_close_file(data, cache_comment):
    """
    Close file safely
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.write('data {} {}\n'.format(data, cache_comment))
    return

def save_and_exit(signum, frame):
    """
    Save all current data and safely exit the program
    """
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.write('data {} {}\n'.format(time.time(), 0))
    exit(1)

# Set up signal to handle saving data and exiting safely
signal.signal(signal.SIGINT, save_and_exit)

def main():
    try:
        validate_environment()
        ensure_cache_directory()
        
        # Your existing main code here
        
    except ValueError as e:
        print(f"Validation Error: {str(e)}")
        sys.exit(1)
    except EnvironmentError as e:
        print(f"Environment Error: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected Error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()