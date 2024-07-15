import datetime
from dateutil import relativedelta
import requests
import os
from xml.dom import minidom
import time
import hashlib

# Personal access token with permissions: read:enterprise, read:org, read:repo_hook, read:user, repo
HEADERS = {'Authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


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


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(f"{func_name} has failed with a {request.status_code}, {request.text}, {QUERY_COUNT}")


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
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


def query_count(function_name):
    """
    Keeps track of how many queries have been made to GitHub
    """
    QUERY_COUNT[function_name] += 1
    QUERY_COUNT['total'] += 1


def stars_counter(edges, star_total=0):
    """
    Count the number of stars in each repo
    """
    for repo in edges:
        star_total += repo['node']['stargazers']['totalCount']
    return star_total


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
