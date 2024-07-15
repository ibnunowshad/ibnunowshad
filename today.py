import datetime
from dateutil import relativedelta
import requests
import os
from xml.dom import minidom
import time
import hashlib
import signal

# Personal access token with permissions: read:enterprise, read:org, read:repo_hook, read:user, repo
HEADERS = {'Authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
# OWNER_ID = os.environ['OWNER_ID']


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


def flush_cache(edges, filename, comment_size):
    """
    Write initial data to the cache file.
    Each line in the cache file represents a repository and its commit count.
    """
    with open(filename, 'w') as f:
        # Add the comment block to the cache file
        if comment_size > 0:
            for _ in range(comment_size):
                f.write('This line is a comment block. Write whatever you want here.\n')

        for edge in edges:
            repo_hash = hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            default_branch_ref = edge['node'].get('defaultBranchRef')
            if not default_branch_ref:
                commit_count = 0
            else:
                target = default_branch_ref.get('target')
                if not target:
                    commit_count = 0
                else:
                    history = target.get('history')
                    if not history:
                        commit_count = 0
                    else:
                        commit_count = history.get('totalCount', 0)
            f.write(f"{repo_hash} {commit_count} 0 0 0\n")  # Initialize LOC values as 0


# Add the flush_cache function to the script where it was called before
def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                default_branch_ref = edges[index]['node'].get('defaultBranchRef')
                if not default_branch_ref:
                    continue

                target = default_branch_ref.get('target')
                if not target:
                    continue

                history = target.get('history')
                if not history:
                    continue

                total_count = history.get('totalCount')
                if total_count is None:
                    continue

                if int(commit_count) != total_count:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(total_count) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    svg = minidom.parse(filename)
    f = open(filename, mode='w', encoding='utf-8')
    tspan = svg.getElementsByTagName('tspan')
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


import requests

def add_archive():
    """
    Function to handle querying and processing archived data.
    This implementation uses a mock API call for demonstration.
    """
    query = """
    {
        user(login: "YOUR_USERNAME") {
            repositories(first: 100, isArchived: true) {
                edges {
                    node {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                history {
                                    totalCount
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """

    response = requests.post(url, json={'query': query}, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Query failed to run with a {response.status_code}: {response.text}")

    data = response.json()
    archived_data = {}
    for edge in data['data']['user']['repositories']['edges']:
        repo_hash = hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest()
        total_count = edge['node']['defaultBranchRef']['target']['history']['totalCount']
        archived_data[repo_hash] = total_count

    return archived_data



# Set up signal to handle saving data and exiting safely
signal.signal(signal.SIGINT, save_and_exit)


def main():
    """
    Main function to execute the script.
    """
    if __name__ == '__main__':
        print('Calculation times:')

        # Perform initial calculations
        user_data, user_time = perf_counter(user_getter, USER_NAME)
        OWNER_ID, acc_date = user_data
        formatter('account data', user_time)

        age_data, age_time = perf_counter(daily_readme, datetime.datetime(1987, 5, 9))
        formatter('age calculation', age_time)

        total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
        formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)

        commit_data, commit_time = perf_counter(commit_counter, 7)
        star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
        repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
        contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

        # Handle archived data for the specific user
        if OWNER_ID == {'id': 'MDQ6VXNlcjUxMjE4NTI='}:  # Only calculate for user ibnunowshad
            archived_data = add_archive()
            if archived_data:
                total_loc[0] += archived_data['LOC']
                contrib_data += archived_data['contributed_repos']
                commit_data += archived_data['commits']

        commit_data = formatter('commit counter', commit_time, commit_data, 7)
        star_data = formatter('star counter', star_time, star_data)
        repo_data = formatter('my repositories', repo_time, repo_data, 2)
        contrib_data = formatter('contributed repos', contrib_time, contrib_data, 2)
        follower_data = formatter('follower counter', follower_time, follower_data, 4)

        for index in range(len(total_loc) - 1):
            total_loc[index] = '{:,}'.format(total_loc[index])  # Format added, deleted, and total LOC

        svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data,
                      total_loc[:-1])
        svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data,
                      total_loc[:-1])

        # Print total function time
        total_function_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time
        print(f"Total function time: {'%.4f' % total_function_time} s")

        # Print total GitHub GraphQL API calls
        total_api_calls = sum(QUERY_COUNT.values())
        print(f"Total GitHub GraphQL API calls: {total_api_calls}")

        # Print individual API call counts
        for funct_name, count in QUERY_COUNT.items():
            print(f"{funct_name}: {count}")

# Call main function when running the script
if __name__ == "__main__":
    main()
